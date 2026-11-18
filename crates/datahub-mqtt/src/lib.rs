//! MQTT-only host adapter; no database dependency or hidden Application.
#![forbid(unsafe_code)]
use datahub_app::{Composition, Demand, Endpoint, Result, invalid};
use datahub_config::{Config, read_document};
use datahub_web::WebContribution;
use modorch::{ComponentRef, FailurePolicy};
use modorch_mqtt::{
    AdminControlConfig, MAX_MQTT_TOML_BYTES, MqttBrokerBundle, MqttBrokerConfig,
    MqttRegisteredComponents, MqttTomlConfig,
};
use std::sync::Arc;

mod web;

pub struct Plan {
    config: MqttBrokerConfig,
    web_bridge: Option<(u64, Arc<()>)>,
}
impl Plan {
    pub fn load(host: &Config) -> Result<Self> {
        let reference = host
            .mqtt
            .as_ref()
            .ok_or_else(|| invalid("missing MQTT configuration reference"))?;
        let path = reference.config_file.canonicalize()?;
        let base = path
            .parent()
            .ok_or_else(|| invalid("MQTT configuration has no parent"))?;
        let document = read_document(&path, MAX_MQTT_TOML_BYTES)?;
        let parsed = MqttTomlConfig::parse(&document, base, None)?;
        let config = parsed
            .into_core_config()
            .ok_or_else(|| invalid("this service requires enabled MQTT Core"))?;
        if (!config.listener.ip().is_loopback() && !config.allow_public_listener_without_tls)
            || !config.anonymous_access
            || config.auth.is_some()
            || config.storage.is_some()
            || config.tls.is_some()
            || config.websocket.is_some()
            || config.admin.is_some()
        {
            return Err(invalid(
                "host MQTT profile requires anonymous MQTT 3.1.1/5.0 memory Core (non-loopback requires allow_public_listener_without_tls=true)",
            ));
        }
        Ok(Self {
            config,
            web_bridge: None,
        })
    }
    pub fn config(&self) -> &MqttBrokerConfig {
        &self.config
    }
    pub fn enable_web_admin(&mut self) -> WebContribution {
        let mut next_id = 2000u64;
        let mut alloc_id = || {
            while self.config.control_subscription_ids.contains(&next_id) {
                next_id += 1;
            }
            let id = next_id;
            next_id += 1;
            id
        };
        let admin_sub_id = alloc_id();
        let bridge_sub_id = alloc_id();
        self.config.admin = Some(AdminControlConfig {
            subscription_id: admin_sub_id,
            allowed_sources: vec![ComponentRef::module(web::BRIDGE_COMPONENT_ID)],
        });
        let capability = Arc::new(());
        self.web_bridge = Some((bridge_sub_id, Arc::clone(&capability)));
        WebContribution {
            service: "mqtt",
            requires: vec![],
            slot: None,
            builder: Some(Arc::new(move |ctx| {
                web::router(Arc::clone(&capability), ctx.event_hub.external())
            })),
        }
    }
    pub fn register(self, composition: &mut Composition) -> Result<Arc<MqttRegisteredComponents>> {
        let tasks = self
            .config
            .max_connections
            .checked_add(2)
            .ok_or_else(|| invalid("MQTT task demand overflow"))?;
        composition.require(Demand {
            tasks,
            timers: 1,
            owner_tasks: tasks,
            ..Default::default()
        })?;
        composition.endpoint("mqtt", self.config.listener);
        let broker_name = self.config.name.clone();
        let registered = Arc::new(
            MqttBrokerBundle::new(self.config)
                .with_failure_policy(FailurePolicy::FailFast)
                .register(composition.builder())?,
        );
        for id in registered.module_ids() {
            composition.enable_module(id);
        }
        if let Some((bridge_sub_id, capability)) = self.web_bridge {
            composition
                .builder()
                .register_module(web::BridgeModuleFactory::new(
                    broker_name,
                    bridge_sub_id,
                    capability,
                ))?;
            composition.enable_module(web::BRIDGE_COMPONENT_ID);
        }
        let observed = registered.clone();
        composition.observe(move |app| {
            if !app.health().ready || !observed.handle().status().active {
                return Err(invalid("MQTT not Ready"));
            }
            let address = observed
                .handle()
                .local_addr()
                .ok_or_else(|| invalid("MQTT active address missing"))?;
            Ok(vec![Endpoint {
                protocol: "mqtt",
                address,
            }])
        });
        Ok(registered)
    }
}
