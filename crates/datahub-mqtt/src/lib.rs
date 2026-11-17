//! MQTT-only host adapter; no database dependency or hidden Application.
#![forbid(unsafe_code)]
use datahub_app::{Composition, Demand, Endpoint, Result, invalid};
use datahub_config::{Config, read_document};
use modorch::FailurePolicy;
use modorch_mqtt::{
    MAX_MQTT_TOML_BYTES, MqttBrokerBundle, MqttBrokerConfig, MqttRegisteredComponents,
    MqttTomlConfig,
};
use std::sync::Arc;

pub struct Plan {
    config: MqttBrokerConfig,
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
            || config.mqtt5
        {
            return Err(invalid(
                "host MQTT profile requires anonymous MQTT3.1.1 memory Core (non-loopback requires allow_public_listener_without_tls=true)",
            ));
        }
        Ok(Self { config })
    }
    pub fn config(&self) -> &MqttBrokerConfig {
        &self.config
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
        let registered = Arc::new(
            MqttBrokerBundle::new(self.config)
                .with_failure_policy(FailurePolicy::FailFast)
                .register(composition.builder())?,
        );
        for id in registered.module_ids() {
            composition.enable_module(id);
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
