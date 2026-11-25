//! FedEngine-only host adapter; business DTOs remain owned by the Facade.
#![forbid(unsafe_code)]
use datahub_app::{Composition, Demand, Endpoint, Result, invalid};
use datahub_config::{Config, MAX_CONFIG_BYTES, read_document, resolve};
use datahub_web::{RouterSlot, WebContribution};
use modorch::ComponentRef;
use modorch_fedengine::{
    FedEngineBundle, FedEngineConfig, FedEngineProfile, FedEngineRegisteredComponents,
};
use std::{net::SocketAddr, path::Path, sync::Arc};

pub struct Plan {
    config: FedEngineConfig,
    profile: FedEngineProfile,
    pgwire: bool,
    http: bool,
}
impl Plan {
    pub fn load(host: &Config) -> Result<Self> {
        let reference = host
            .fedengine
            .as_ref()
            .ok_or_else(|| invalid("missing FedEngine configuration reference"))?;
        let path = reference.config_file.canonicalize()?;
        let base = path
            .parent()
            .ok_or_else(|| invalid("FedEngine configuration has no parent"))?;
        let text = read_document(&path, MAX_CONFIG_BYTES)?;
        let mut config: FedEngineConfig =
            toml::from_str(&text).map_err(|_| invalid("invalid FedEngine TOML"))?;
        if config.application.name != host.application.name {
            return Err(invalid("FedEngine and host application names must match"));
        }
        config.validate_for_ingress(reference.pgwire, reference.management_api)?;
        if reference.management_api && !config.management.enabled {
            return Err(invalid("management HTTP requires managed storage"));
        }
        // Library paths are frozen against the library configuration's directory.
        let freeze = |value: &mut String| -> Result<()> {
            *value = resolve(base, Path::new(value))?
                .into_os_string()
                .into_string()
                .map_err(|_| invalid("configuration paths must be UTF-8"))?;
            Ok(())
        };
        if config.duckdb.database_path != ":memory:" {
            freeze(&mut config.duckdb.database_path)?;
        }
        freeze(&mut config.duckdb.temp_directory)?;
        freeze(&mut config.management.credentials_directory)?;
        if let Some(value) = &mut config.duckdb.extension_directory {
            freeze(value)?;
        }
        if let Some(value) = &mut config.duckdb.plugin_extension_directory {
            freeze(value)?;
        }
        if reference.pgwire {
            for value in [&mut config.pgwire.tls_cert, &mut config.pgwire.tls_key]
                .into_iter()
                .flatten()
            {
                freeze(value)?;
            }
        }
        for (enabled, listen) in [
            (reference.pgwire, &config.pgwire.listen),
            (reference.management_api, &config.management.listen),
        ] {
            if enabled
                && !reference.allow_public_ingress
                && !listen.parse::<SocketAddr>()?.ip().is_loopback()
            {
                return Err(invalid(
                    "first host profile requires loopback database ingress unless allow_public_ingress = true",
                ));
            }
        }
        let profile = if reference.pgwire {
            FedEngineProfile::PgServer {
                management_api: reference.management_api,
            }
        } else {
            FedEngineProfile::ManagedServer { pgwire: false }
        };
        Ok(Self {
            config,
            profile,
            pgwire: reference.pgwire,
            http: reference.management_api,
        })
    }
    pub fn config(&self) -> &FedEngineConfig {
        &self.config
    }
    pub fn management_enabled(&self) -> bool {
        self.http
    }
    pub fn management_listen(&self) -> Result<SocketAddr> {
        Ok(self.config.management.listen.parse()?)
    }
    pub fn web_contribution(&self) -> Option<(RouterSlot, WebContribution)> {
        if !self.http {
            return None;
        }
        let slot: RouterSlot = Arc::default();
        let contribution = WebContribution {
            service: "fedengine",
            requires: vec![ComponentRef::initiator("management-api")],
            slot: Some(Arc::clone(&slot)),
            builder: None,
        };
        Some((slot, contribution))
    }
    pub fn register(
        self,
        composition: &mut Composition,
    ) -> Result<Arc<FedEngineRegisteredComponents>> {
        self.register_with_router_slot(composition, None)
    }
    pub fn register_with_router_slot(
        self,
        composition: &mut Composition,
        router_slot: Option<RouterSlot>,
    ) -> Result<Arc<FedEngineRegisteredComponents>> {
        if self.pgwire {
            composition.endpoint("pgwire", self.config.pgwire.listen.parse()?);
        }
        let hosted_externally = router_slot.is_some();
        if self.http && !hosted_externally {
            composition.endpoint("management", self.config.management.listen.parse()?);
        }
        let mut bundle = FedEngineBundle::new(self.config).profile(self.profile);
        if let Some(slot) = router_slot {
            bundle = bundle.with_management_router_slot(slot);
        }
        let registered = Arc::new(bundle.register(composition.builder())?);
        let demand = registered.resource_requirements();
        let tasks = demand
            .max_tasks_per_owner
            .checked_mul(registered.components().count())
            .ok_or_else(|| invalid("FedEngine task demand overflow"))?;
        composition.require(Demand {
            tasks,
            timers: 1,
            owner_tasks: demand.max_tasks_per_owner,
            blocking_threads: demand.blocking_io_threads,
            blocking_capacity: demand.blocking_io_capacity,
        })?;
        for id in registered.initiator_ids() {
            composition.enable_initiator(id);
        }
        for id in registered.module_ids() {
            composition.enable_module(id);
        }
        let observed = registered.clone();
        composition.observe(move |app| {
            observed.observe(app)?;
            if !observed.handle().status().ready {
                return Err(invalid("FedEngine not Ready"));
            }
            let mut endpoints = vec![];
            if let Some(address) = observed.handle().pgwire_address() {
                endpoints.push(Endpoint {
                    protocol: "pgwire",
                    address,
                });
            }
            if let Some(address) = observed.handle().management_address() {
                endpoints.push(Endpoint {
                    protocol: "management",
                    address,
                });
            }
            Ok(endpoints)
        });
        Ok(registered)
    }
}
