//! Service composition; the common runner owns the only Application lifecycle.
#![forbid(unsafe_code)]
use datahub_app::{Composition, Result};
use datahub_config::{Command, Config, Service};

fn main() -> Result<()> {
    let command = datahub_config::command(std::env::args().skip(1))?;
    let (path, check) = match command {
        Command::Help => {
            println!("datahub-server --config FILE [--check-config] | --version");
            return Ok(());
        }
        Command::Version => {
            println!(
                "datahub-server {} / Modorch 0.0.3 / development",
                env!("CARGO_PKG_VERSION")
            );
            return Ok(());
        }
        Command::Run(path) => (path, false),
        Command::Check(path) => (path, true),
    };
    let config = Config::load(&path, Service::Combined)?;
    let mut composition = Composition::new(config.clone());
    let mut mqtt_plan = datahub_mqtt::Plan::load(&config)?;
    let fed_plan = datahub_fedengine::Plan::load(&config)?;
    if fed_plan.management_enabled() || config.web.enabled {
        let listen = match config.web.listen {
            Some(addr) => addr,
            None if fed_plan.management_enabled() => fed_plan.management_listen()?,
            None => {
                return Err(datahub_app::invalid(
                    "web.listen is required when fedengine.management_api is disabled",
                ));
            }
        };
        let mut contributions = Vec::new();
        let mut fed_slot = None;
        if let Some((slot, contrib)) = fed_plan.web_contribution() {
            fed_slot = Some(slot);
            contributions.push(contrib);
        }
        contributions.push(mqtt_plan.enable_web_admin());
        mqtt_plan.register(&mut composition)?;
        fed_plan.register_with_router_slot(&mut composition, fed_slot)?;
        datahub_web::WebGatewayPlan::new(
            listen,
            config.web.bearer_token_env.clone(),
            config.application.name.clone(),
            contributions,
        )?
        .register(&mut composition)?;
    } else {
        mqtt_plan.register(&mut composition)?;
        fed_plan.register(&mut composition)?;
    }
    let demand = composition.check()?;
    if check {
        println!("configuration valid; demand={demand:?}; runtime not started");
        return Ok(());
    }
    datahub_app::init_logging()?;
    let prepared = composition.build()?;
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(config.runtime.async_threads)
        .max_blocking_threads(config.runtime.tokio_blocking_threads)
        .enable_all()
        .build()?;
    runtime.block_on(datahub_app::serve(prepared))
}
