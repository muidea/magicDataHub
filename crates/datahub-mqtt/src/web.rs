use axum::{
    Json, Router,
    extract::{Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use modorch::{
    ComponentConfig, ComponentDescriptor, ComponentRef, Destination, ExternalEventHub,
    FrameworkResult, HandlerContext, HandlerRegistration, HandlerReply, Lifecycle, MessageEnvelope,
    MessageKind, MessageOptions, Module, ModuleFactory, SerialHandler, SubscriptionSpec,
    async_trait,
};
use modorch_mqtt::{ADMIN_CONTROL_TOPIC, AdminError, AdminReply, AdminRequest, AdminResult, Qos};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{sync::Arc, time::Duration};

const ADMIN_TIMEOUT: Duration = Duration::from_secs(5);
pub(crate) const BRIDGE_COMPONENT_ID: &str = "mqtt-web-admin";
const BRIDGE_TOPIC: &str = "$modorch/mqtt/web-bridge/v1";

#[derive(Clone)]
struct Forward {
    capability: Arc<()>,
    request: AdminRequest,
}

pub(crate) struct BridgeModuleFactory {
    broker: String,
    subscription_id: u64,
    capability: Arc<()>,
}

impl BridgeModuleFactory {
    pub(crate) fn new(broker: String, subscription_id: u64, capability: Arc<()>) -> Self {
        Self {
            broker,
            subscription_id,
            capability,
        }
    }
    fn descriptor_spec(&self) -> ComponentDescriptor {
        let mut d = ComponentDescriptor::new(ComponentRef::module(BRIDGE_COMPONENT_ID));
        d.requires.push(ComponentRef::module(&self.broker));
        d.queue_capacity = 64;
        d
    }
}

impl ModuleFactory for BridgeModuleFactory {
    fn descriptor(&self) -> ComponentDescriptor {
        self.descriptor_spec()
    }
    fn create(&self, _: &ComponentConfig) -> FrameworkResult<Box<dyn Module>> {
        Ok(Box::new(BridgeModule {
            broker: self.broker.clone(),
            subscription_id: self.subscription_id,
            capability: Arc::clone(&self.capability),
        }))
    }
}

struct BridgeModule {
    broker: String,
    subscription_id: u64,
    capability: Arc<()>,
}

#[async_trait]
impl Lifecycle for BridgeModule {}

impl Module for BridgeModule {
    fn descriptor(&self) -> ComponentDescriptor {
        let mut d = ComponentDescriptor::new(ComponentRef::module(BRIDGE_COMPONENT_ID));
        d.requires.push(ComponentRef::module(&self.broker));
        d.queue_capacity = 64;
        d
    }
    fn handlers(&mut self) -> FrameworkResult<Option<HandlerRegistration>> {
        Ok(Some(HandlerRegistration::Serial {
            handler: Box::new(BridgeHandler {
                broker: self.broker.clone(),
                capability: Arc::clone(&self.capability),
            }),
            subscriptions: vec![SubscriptionSpec::new(
                self.subscription_id,
                self.descriptor().component(),
                BRIDGE_TOPIC,
            )],
        }))
    }
}

struct BridgeHandler {
    broker: String,
    capability: Arc<()>,
}

fn request_cost(request: &AdminRequest) -> usize {
    match request {
        AdminRequest::GetSession { client_id }
        | AdminRequest::DisconnectClient { client_id, .. } => 256 + client_id.len(),
        AdminRequest::ListClients { after, .. } => 256 + after.as_ref().map_or(0, String::len),
        AdminRequest::ListSubscriptions {
            client_id, after, ..
        } => 256 + client_id.len() + after.as_ref().map_or(0, String::len),
        AdminRequest::ListTopicFilters { after, .. } => 256 + after.as_ref().map_or(0, String::len),
        AdminRequest::MatchTopic { topic }
        | AdminRequest::GetRetained { topic }
        | AdminRequest::DeleteRetained { topic } => 256 + topic.len(),
        AdminRequest::ListRetained { prefix, after, .. } => {
            256 + prefix.as_ref().map_or(0, String::len) + after.as_ref().map_or(0, String::len)
        }
        AdminRequest::ListActiveTopics { search, .. } => {
            256 + search.as_ref().map_or(0, String::len)
        }
        AdminRequest::AdminPublish { topic, payload, .. } => 256 + topic.len() + payload.len(),
        AdminRequest::BrokerStats | AdminRequest::ListEvents { .. } => 256,
    }
}

#[async_trait]
impl SerialHandler for BridgeHandler {
    async fn handle(
        &mut self,
        ctx: &HandlerContext,
        message: MessageEnvelope,
    ) -> FrameworkResult<HandlerReply> {
        if message.kind != MessageKind::Request
            || message.destination != Destination::Component(ctx.component.clone())
        {
            return Ok(Arc::new(Err::<AdminReply, _>(AdminError::NotAuthorized)));
        }
        let forward = message.payload::<Forward>()?;
        if !Arc::ptr_eq(&forward.capability, &self.capability) {
            return Ok(Arc::new(Err::<AdminReply, _>(AdminError::NotAuthorized)));
        }
        let cost = request_cost(&forward.request);
        Ok(ctx
            .event_hub
            .request::<_, AdminResult>(
                ADMIN_CONTROL_TOPIC,
                Destination::Component(ComponentRef::module(&self.broker)),
                forward.request.clone(),
                MessageOptions {
                    payload_bytes: Some(cost),
                    ..Default::default()
                },
            )?
            .wait(ADMIN_TIMEOUT)
            .await?)
    }
}

#[derive(Clone)]
struct MqttWebState {
    capability: Arc<()>,
    hub: ExternalEventHub,
}

pub(crate) fn router(capability: Arc<()>, hub: ExternalEventHub) -> Router {
    let state = MqttWebState { capability, hub };
    Router::new()
        .route("/manage/mqtt.js", get(mqtt_javascript))
        .route("/api/v1/mqtt/stats", get(get_stats))
        .route("/api/v1/mqtt/clients", get(get_clients))
        .route("/api/v1/mqtt/session", get(get_session))
        .route("/api/v1/mqtt/subscriptions", get(get_subscriptions))
        .route("/api/v1/mqtt/disconnect", post(post_disconnect))
        .route("/api/v1/mqtt/events", get(get_events))
        .route("/api/v1/mqtt/topics/filters", get(get_topic_filters))
        .route("/api/v1/mqtt/topics/match", get(get_topic_match))
        .route("/api/v1/mqtt/topics/active", get(get_active_topics))
        .route(
            "/api/v1/mqtt/retained",
            get(get_retained_list).delete(delete_retained),
        )
        .route("/api/v1/mqtt/retained/detail", get(get_retained_detail))
        .route("/api/v1/mqtt/publish", post(post_admin_publish))
        .with_state(state)
}

async fn mqtt_javascript() -> Response {
    datahub_web::asset_response(
        "text/javascript; charset=utf-8",
        include_str!("../web/mqtt.js"),
    )
}

fn api_error(status: StatusCode, message: impl Into<String>, code: &'static str) -> Response {
    (
        status,
        Json(json!({ "error": message.into(), "code": code })),
    )
        .into_response()
}

fn unexpected_reply() -> Response {
    api_error(
        StatusCode::INTERNAL_SERVER_ERROR,
        "意外响应类型",
        "INTERNAL",
    )
}

fn admin_error_response(error: AdminError) -> Response {
    match error {
        AdminError::NotAuthorized => {
            api_error(StatusCode::FORBIDDEN, "MQTT 管理访问被拒绝", "FORBIDDEN")
        }
        AdminError::InvalidRequest => api_error(
            StatusCode::BAD_REQUEST,
            "MQTT 管理请求参数无效",
            "BAD_REQUEST",
        ),
        AdminError::Unavailable => api_error(
            StatusCode::SERVICE_UNAVAILABLE,
            "MQTT Broker 当前不可用",
            "UNAVAILABLE",
        ),
    }
}

async fn call_admin(
    state: &MqttWebState,
    request: AdminRequest,
) -> std::result::Result<AdminReply, Response> {
    let cost = request_cost(&request);
    let options = MessageOptions {
        payload_bytes: Some(cost),
        ..Default::default()
    };
    let ticket = state
        .hub
        .request::<_, AdminResult>(
            BRIDGE_TOPIC,
            Destination::Component(ComponentRef::module(BRIDGE_COMPONENT_ID)),
            Forward {
                capability: Arc::clone(&state.capability),
                request,
            },
            options,
        )
        .map_err(|_| {
            api_error(
                StatusCode::SERVICE_UNAVAILABLE,
                "MQTT 管理通道不可用",
                "HUB_ERROR",
            )
        })?;
    let reply = ticket
        .wait(ADMIN_TIMEOUT)
        .await
        .map_err(|_| api_error(StatusCode::GATEWAY_TIMEOUT, "MQTT 管理请求超时", "TIMEOUT"))?;
    reply.as_ref().clone().map_err(admin_error_response)
}

async fn call_json<T: Serialize>(
    state: &MqttWebState,
    request: AdminRequest,
    map: impl FnOnce(AdminReply) -> Option<T>,
) -> Response {
    match call_admin(state, request).await {
        Ok(reply) => match map(reply) {
            Some(value) => (StatusCode::OK, Json(value)).into_response(),
            None => unexpected_reply(),
        },
        Err(resp) => resp,
    }
}

async fn get_stats(State(state): State<MqttWebState>) -> Response {
    call_json(&state, AdminRequest::BrokerStats, |reply| match reply {
        AdminReply::Stats(stats) => Some(stats),
        _ => None,
    })
    .await
}

#[derive(Deserialize)]
struct ClientsQuery {
    after: Option<String>,
    limit: Option<usize>,
    search: Option<String>,
    online_only: Option<bool>,
}

async fn get_clients(State(state): State<MqttWebState>, Query(q): Query<ClientsQuery>) -> Response {
    let limit = q.limit.unwrap_or(50).clamp(1, 256);
    let search = q.search.as_deref().map(str::trim).unwrap_or("");
    let online_only = q.online_only.unwrap_or(false);
    if search.is_empty() && !online_only {
        return call_json(
            &state,
            AdminRequest::ListClients {
                after: q.after,
                limit,
            },
            |reply| match reply {
                AdminReply::Clients { items, next } => {
                    Some(json!({ "items": items, "next": next }))
                }
                _ => None,
            },
        )
        .await;
    }

    let search_lower = search.to_lowercase();
    let mut cursor = q.after;
    let mut matched = Vec::with_capacity(limit);
    let mut next_cursor = None;
    for _ in 0..8 {
        match call_admin(
            &state,
            AdminRequest::ListClients {
                after: cursor,
                limit: 128,
            },
        )
        .await
        {
            Ok(AdminReply::Clients { items, next }) => {
                for item in items {
                    let is_online = !online_only || item.online;
                    let matches_search = search_lower.is_empty()
                        || item.client_id.to_lowercase().contains(&search_lower)
                        || item
                            .peer_addr
                            .as_deref()
                            .is_some_and(|p| p.to_lowercase().contains(&search_lower));
                    if is_online && matches_search {
                        matched.push(item);
                        if matched.len() == limit {
                            break;
                        }
                    }
                }
                next_cursor = next.clone();
                if matched.len() >= limit || next.is_none() {
                    break;
                }
                cursor = next;
            }
            Ok(_) => return unexpected_reply(),
            Err(resp) => return resp,
        }
    }
    (
        StatusCode::OK,
        Json(json!({ "items": matched, "next": next_cursor })),
    )
        .into_response()
}

#[derive(Deserialize)]
struct SessionQuery {
    client_id: String,
}

async fn get_session(State(state): State<MqttWebState>, Query(q): Query<SessionQuery>) -> Response {
    call_json(
        &state,
        AdminRequest::GetSession {
            client_id: q.client_id,
        },
        |reply| match reply {
            AdminReply::Session(session) => Some(json!({ "session": session })),
            _ => None,
        },
    )
    .await
}

#[derive(Deserialize)]
struct SubscriptionsQuery {
    client_id: String,
    after: Option<String>,
    limit: Option<usize>,
}

async fn get_subscriptions(
    State(state): State<MqttWebState>,
    Query(q): Query<SubscriptionsQuery>,
) -> Response {
    let limit = q.limit.unwrap_or(100).clamp(1, 256);
    call_json(
        &state,
        AdminRequest::ListSubscriptions {
            client_id: q.client_id,
            after: q.after,
            limit,
        },
        |reply| match reply {
            AdminReply::Subscriptions { items, next } => {
                Some(json!({ "items": items, "next": next }))
            }
            _ => None,
        },
    )
    .await
}

#[derive(Deserialize)]
struct DisconnectBody {
    client_id: String,
    connection_id: u64,
}

async fn post_disconnect(
    State(state): State<MqttWebState>,
    Json(body): Json<DisconnectBody>,
) -> Response {
    call_json(
        &state,
        AdminRequest::DisconnectClient {
            client_id: body.client_id,
            expected_connection_id: body.connection_id,
        },
        |reply| match reply {
            AdminReply::Disconnected(disconnected) => Some(json!({ "disconnected": disconnected })),
            _ => None,
        },
    )
    .await
}

#[derive(Deserialize)]
struct EventsQuery {
    limit: Option<usize>,
}

async fn get_events(State(state): State<MqttWebState>, Query(q): Query<EventsQuery>) -> Response {
    let limit = q.limit.unwrap_or(100).clamp(1, 256);
    call_json(
        &state,
        AdminRequest::ListEvents { limit },
        |reply| match reply {
            AdminReply::Events(items) => Some(json!({ "items": items })),
            _ => None,
        },
    )
    .await
}

#[derive(Deserialize)]
struct TopicFiltersQuery {
    after: Option<String>,
    limit: Option<usize>,
    search: Option<String>,
}

async fn get_topic_filters(
    State(state): State<MqttWebState>,
    Query(q): Query<TopicFiltersQuery>,
) -> Response {
    let limit = q.limit.unwrap_or(100).clamp(1, 256);
    let search = q
        .search
        .as_deref()
        .map(str::trim)
        .unwrap_or("")
        .to_lowercase();
    call_json(
        &state,
        AdminRequest::ListTopicFilters {
            after: q.after,
            limit,
        },
        |reply| match reply {
            AdminReply::TopicFilters { mut items, next } => {
                if !search.is_empty() {
                    items.retain(|item| {
                        item.filter.to_lowercase().contains(&search)
                            || item
                                .subscribers
                                .iter()
                                .any(|s| s.client_id.to_lowercase().contains(&search))
                    });
                }
                Some(json!({ "items": items, "next": next }))
            }
            _ => None,
        },
    )
    .await
}

#[derive(Deserialize)]
struct TopicQuery {
    topic: String,
}

async fn get_topic_match(
    State(state): State<MqttWebState>,
    Query(q): Query<TopicQuery>,
) -> Response {
    call_json(
        &state,
        AdminRequest::MatchTopic { topic: q.topic },
        |reply| match reply {
            AdminReply::TopicMatch(result) => Some(result),
            _ => None,
        },
    )
    .await
}

#[derive(Deserialize)]
struct ActiveTopicsQuery {
    search: Option<String>,
    limit: Option<usize>,
}

async fn get_active_topics(
    State(state): State<MqttWebState>,
    Query(q): Query<ActiveTopicsQuery>,
) -> Response {
    let limit = q.limit.unwrap_or(100).clamp(1, 256);
    call_json(
        &state,
        AdminRequest::ListActiveTopics {
            search: q.search,
            limit,
        },
        |reply| match reply {
            AdminReply::ActiveTopics(items) => Some(json!({ "items": items })),
            _ => None,
        },
    )
    .await
}

#[derive(Deserialize)]
struct RetainedQuery {
    prefix: Option<String>,
    after: Option<String>,
    limit: Option<usize>,
}

async fn get_retained_list(
    State(state): State<MqttWebState>,
    Query(q): Query<RetainedQuery>,
) -> Response {
    let limit = q.limit.unwrap_or(100).clamp(1, 256);
    call_json(
        &state,
        AdminRequest::ListRetained {
            prefix: q.prefix.filter(|p| !p.is_empty()),
            after: q.after,
            limit,
        },
        |reply| match reply {
            AdminReply::RetainedList { items, next } => {
                Some(json!({ "items": items, "next": next }))
            }
            _ => None,
        },
    )
    .await
}

async fn get_retained_detail(
    State(state): State<MqttWebState>,
    Query(q): Query<TopicQuery>,
) -> Response {
    call_json(
        &state,
        AdminRequest::GetRetained { topic: q.topic },
        |reply| match reply {
            AdminReply::RetainedDetail(item) => Some(json!({ "item": item })),
            _ => None,
        },
    )
    .await
}

async fn delete_retained(
    State(state): State<MqttWebState>,
    Query(q): Query<TopicQuery>,
) -> Response {
    call_json(
        &state,
        AdminRequest::DeleteRetained { topic: q.topic },
        |reply| match reply {
            AdminReply::RetainedDeleted(deleted) => Some(json!({ "deleted": deleted })),
            _ => None,
        },
    )
    .await
}

#[derive(Deserialize)]
struct PublishBody {
    topic: String,
    payload: String,
    qos: u8,
    #[serde(default)]
    retain: bool,
}

async fn post_admin_publish(
    State(state): State<MqttWebState>,
    Json(body): Json<PublishBody>,
) -> Response {
    let Ok(qos) = Qos::try_from(body.qos) else {
        return api_error(
            StatusCode::BAD_REQUEST,
            "无效 QoS 等级（仅支持 0/1/2）",
            "BAD_REQUEST",
        );
    };
    call_json(
        &state,
        AdminRequest::AdminPublish {
            topic: body.topic,
            payload: body.payload.into_bytes(),
            qos,
            retain: body.retain,
        },
        |reply| match reply {
            AdminReply::Published { matched_sessions } => {
                Some(json!({ "matched_sessions": matched_sessions }))
            }
            _ => None,
        },
    )
    .await
}
