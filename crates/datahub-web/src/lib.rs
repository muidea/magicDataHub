//! Unified composable Web management gateway without business library dependencies.
#![forbid(unsafe_code)]
use async_trait::async_trait;
use axum::{
    Json, Router,
    body::Body,
    extract::State,
    http::{HeaderMap, HeaderValue, Method, Request, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::get,
};
use datahub_app::{Composition, Demand, Endpoint, Result, invalid};
use modorch::{
    ComponentConfig, ComponentDescriptor, ComponentRef, FrameworkError, FrameworkResult, Initiator,
    InitiatorFactory, Lifecycle, RuntimeContext, SetupContext, ShutdownContext,
};
use serde::Serialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    net::SocketAddr,
    sync::{Arc, Mutex},
};
use tokio::net::TcpSocket;
use tokio_util::sync::CancellationToken;

pub const MANAGEMENT_WEB_ID: &str = "management-web";

pub type RouterSlot = Arc<Mutex<Option<Router>>>;
pub type RouterBuilder = Arc<dyn Fn(&RuntimeContext) -> Router + Send + Sync>;

/// Service module plugged into the unified Web console.
#[derive(Clone)]
pub struct WebContribution {
    pub service: &'static str,
    pub requires: Vec<ComponentRef>,
    pub slot: Option<RouterSlot>,
    pub builder: Option<RouterBuilder>,
}

/// Shared read-only listener status for observation.
#[derive(Default)]
pub struct WebListenerStatus {
    address: Mutex<Option<SocketAddr>>,
}
impl WebListenerStatus {
    pub fn address(&self) -> Option<SocketAddr> {
        *self.address.lock().unwrap()
    }
}

pub struct WebGatewayPlan {
    listen: SocketAddr,
    bearer_token_env: Option<String>,
    application_name: String,
    contributions: Vec<WebContribution>,
}

impl WebGatewayPlan {
    pub fn new(
        listen: SocketAddr,
        bearer_token_env: Option<String>,
        application_name: String,
        contributions: Vec<WebContribution>,
    ) -> Result<Self> {
        if contributions.is_empty() {
            return Err(invalid(
                "unified Web gateway requires at least one service contribution",
            ));
        }
        Ok(Self {
            listen,
            bearer_token_env,
            application_name,
            contributions,
        })
    }

    pub fn register(self, composition: &mut Composition) -> Result<Arc<WebListenerStatus>> {
        composition.require(Demand {
            tasks: 4,
            timers: 0,
            owner_tasks: 4,
            ..Default::default()
        })?;
        composition.endpoint("management", self.listen);
        let status = Arc::new(WebListenerStatus::default());
        let services: Vec<&'static str> = self.contributions.iter().map(|c| c.service).collect();
        let factory = WebGatewayFactory {
            listen: self.listen,
            bearer_token_env: self.bearer_token_env,
            application_name: self.application_name,
            services,
            contributions: self.contributions,
            status: Arc::clone(&status),
        };
        composition.builder().register_initiator(factory)?;
        composition.enable_initiator(MANAGEMENT_WEB_ID);
        let observed = Arc::clone(&status);
        composition.observe(move |app| {
            if !app.health().ready {
                return Err(invalid("management web gateway not Ready"));
            }
            let address = observed
                .address()
                .ok_or_else(|| invalid("management web listener address missing"))?;
            Ok(vec![Endpoint {
                protocol: "management",
                address,
            }])
        });
        Ok(status)
    }
}

struct WebGatewayFactory {
    listen: SocketAddr,
    bearer_token_env: Option<String>,
    application_name: String,
    services: Vec<&'static str>,
    contributions: Vec<WebContribution>,
    status: Arc<WebListenerStatus>,
}

impl WebGatewayFactory {
    fn descriptor(&self) -> ComponentDescriptor {
        let mut d = ComponentDescriptor::new(ComponentRef::initiator(MANAGEMENT_WEB_ID));
        for c in &self.contributions {
            d.requires.extend(c.requires.iter().cloned());
        }
        d
    }
}

impl InitiatorFactory for WebGatewayFactory {
    fn descriptor(&self) -> ComponentDescriptor {
        self.descriptor()
    }
    fn create(&self, _: &ComponentConfig) -> FrameworkResult<Box<dyn Initiator>> {
        Ok(Box::new(WebGatewayInitiator {
            listen: self.listen,
            bearer_token_env: self.bearer_token_env.clone(),
            application_name: self.application_name.clone(),
            services: self.services.clone(),
            contributions: self.contributions.clone(),
            status: Arc::clone(&self.status),
            socket: None,
            token_sha256: None,
            stop: CancellationToken::new(),
        }))
    }
}

struct WebGatewayInitiator {
    listen: SocketAddr,
    bearer_token_env: Option<String>,
    application_name: String,
    services: Vec<&'static str>,
    contributions: Vec<WebContribution>,
    status: Arc<WebListenerStatus>,
    socket: Option<TcpSocket>,
    token_sha256: Option<[u8; 32]>,
    stop: CancellationToken,
}

#[async_trait]
impl Lifecycle for WebGatewayInitiator {
    async fn setup(&mut self, _: &SetupContext) -> FrameworkResult<()> {
        if let Some(env_name) = &self.bearer_token_env {
            let value = std::env::var(env_name).map_err(|_| {
                FrameworkError::InvalidConfig(format!(
                    "missing management bearer token environment variable: {env_name}"
                ))
            })?;
            if value.len() < 16
                || value.len() > 256
                || !value.bytes().all(|b| (0x21..=0x7e).contains(&b))
            {
                return Err(FrameworkError::InvalidConfig(
                    "management bearer token must be 16..=256 printable ASCII bytes without whitespace"
                        .into(),
                ));
            }
            let digest: [u8; 32] = Sha256::digest(value.as_bytes()).into();
            self.token_sha256 = Some(digest);
        }
        let socket = if self.listen.is_ipv4() {
            TcpSocket::new_v4()
        } else {
            TcpSocket::new_v6()
        }
        .map_err(|e| {
            FrameworkError::InvalidConfig(format!("cannot create management web socket: {e}"))
        })?;
        socket.set_reuseaddr(true).map_err(|e| {
            FrameworkError::InvalidConfig(format!("cannot configure management web socket: {e}"))
        })?;
        socket.bind(self.listen).map_err(|e| {
            FrameworkError::InvalidConfig(format!("cannot bind management web socket: {e}"))
        })?;
        let bound = socket.local_addr().map_err(|_| {
            FrameworkError::InvalidConfig("cannot read management web listener address".into())
        })?;
        *self.status.address.lock().unwrap() = Some(bound);
        self.socket = Some(socket);
        Ok(())
    }

    async fn begin_shutdown(&mut self, _: &ShutdownContext) -> FrameworkResult<()> {
        self.stop.cancel();
        Ok(())
    }

    async fn teardown(&mut self, _: &ShutdownContext) -> FrameworkResult<()> {
        self.socket.take();
        *self.status.address.lock().unwrap() = None;
        Ok(())
    }
}

#[derive(Clone)]
struct SecurityState {
    token_sha256: Option<[u8; 32]>,
}

#[derive(Clone, Serialize)]
struct Capabilities {
    services: Vec<&'static str>,
    auth_required: bool,
    application: String,
}

#[async_trait]
impl Initiator for WebGatewayInitiator {
    fn descriptor(&self) -> ComponentDescriptor {
        let mut d = ComponentDescriptor::new(ComponentRef::initiator(MANAGEMENT_WEB_ID));
        for c in &self.contributions {
            d.requires.extend(c.requires.iter().cloned());
        }
        d
    }

    async fn activate_ingress(&mut self, ctx: &RuntimeContext) -> FrameworkResult<()> {
        let listener = self.socket.take().unwrap().listen(1024).map_err(|e| {
            FrameworkError::InvalidConfig(format!("cannot listen on management web socket: {e}"))
        })?;
        let caps = Capabilities {
            services: self.services.clone(),
            auth_required: self.token_sha256.is_some(),
            application: self.application_name.clone(),
        };
        let mut router = Router::new()
            .route("/", get(web_index))
            .route("/manage", get(web_index))
            .route("/manage/", get(web_index))
            .route("/manage/shell.js", get(web_shell_js))
            .route("/manage/style.css", get(web_style_css))
            .route(
                "/api/v1/capabilities",
                get({
                    let caps = caps.clone();
                    move || async move { Json(caps) }
                }),
            )
            .route(
                "/api/v1/auth/verify",
                get(|| async { Json(json!({ "ok": true })) }),
            );

        for contribution in &self.contributions {
            if let Some(slot) = &contribution.slot {
                let sub = slot.lock().unwrap().take().ok_or_else(|| {
                    FrameworkError::InvalidConfig(format!(
                        "management sub-router slot for {} was not initialized",
                        contribution.service
                    ))
                })?;
                router = router.merge(sub);
            }
            if let Some(builder) = &contribution.builder {
                router = router.merge(builder(ctx));
            }
        }

        let sec_state = SecurityState {
            token_sha256: self.token_sha256,
        };
        let router = router.layer(middleware::from_fn_with_state(sec_state, security_guard));
        let stop = self.stop.clone();
        tracing::info!(
            listen = %self.listen,
            services = ?self.services,
            auth_required = self.token_sha256.is_some(),
            "unified management web gateway listening"
        );
        ctx.tasks.spawn(move |shutdown| async move {
            axum::serve(listener, router)
                .with_graceful_shutdown(async move {
                    tokio::select! { _ = stop.cancelled() => {}, _ = shutdown.cancelled() => {} }
                })
                .await
                .map_err(|_| {
                    FrameworkError::InvalidConfig("unified management web server failed".into())
                })?;
            Ok(())
        })?;
        Ok(())
    }
}

async fn security_guard(
    State(state): State<SecurityState>,
    request: Request<Body>,
    next: Next,
) -> Response {
    let path = request.uri().path();
    if path.starts_with("/api/v1/") && path != "/api/v1/capabilities" {
        if let Some(expected) = state.token_sha256 {
            let provided = request
                .headers()
                .get(header::AUTHORIZATION)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.strip_prefix("Bearer "));
            let authorized = provided.is_some_and(|token| {
                let actual: [u8; 32] = Sha256::digest(token.as_bytes()).into();
                constant_time_eq(&expected, &actual)
            });
            if !authorized {
                return (
                    StatusCode::UNAUTHORIZED,
                    Json(json!({
                        "error": "管理口令无效或未提供",
                        "code": "UNAUTHORIZED"
                    })),
                )
                    .into_response();
            }
        }
        let method = request.method();
        if *method != Method::GET && *method != Method::HEAD && !same_origin(request.headers()) {
            return (
                StatusCode::FORBIDDEN,
                Json(json!({"error": "cross-origin management writes are not allowed"})),
            )
                .into_response();
        }
    }
    next.run(request).await
}

fn constant_time_eq(a: &[u8; 32], b: &[u8; 32]) -> bool {
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

fn same_origin(headers: &HeaderMap) -> bool {
    if headers
        .get("sec-fetch-site")
        .is_some_and(|site| site == "cross-site")
    {
        return false;
    }
    let Some(origin) = headers.get(header::ORIGIN) else {
        return true;
    };
    let Some(host) = headers.get(header::HOST).and_then(|v| v.to_str().ok()) else {
        return false;
    };
    origin.to_str().is_ok_and(|origin| {
        ["http://", "https://"]
            .iter()
            .any(|scheme| origin.strip_prefix(scheme) == Some(host))
    })
}

pub fn asset_response(content_type: &'static str, body: &'static str) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        ),
    );
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    (StatusCode::OK, headers, body).into_response()
}

async fn web_index() -> Response {
    asset_response(
        "text/html; charset=utf-8",
        include_str!("../web/index.html"),
    )
}

async fn web_shell_js() -> Response {
    asset_response(
        "text/javascript; charset=utf-8",
        include_str!("../web/shell.js"),
    )
}

async fn web_style_css() -> Response {
    asset_response("text/css; charset=utf-8", include_str!("../web/style.css"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constant_time_compare_and_same_origin_enforced() {
        let a: [u8; 32] = Sha256::digest(b"secret-token-123456").into();
        let b: [u8; 32] = Sha256::digest(b"secret-token-123456").into();
        let c: [u8; 32] = Sha256::digest(b"other-token-1234567").into();
        assert!(constant_time_eq(&a, &b));
        assert!(!constant_time_eq(&a, &c));

        let mut headers = HeaderMap::new();
        headers.insert(
            header::HOST,
            HeaderValue::from_static("192.168.11.247:25433"),
        );
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("http://192.168.11.247:25433"),
        );
        assert!(same_origin(&headers));

        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("http://evil.example"),
        );
        assert!(!same_origin(&headers));
    }
}
