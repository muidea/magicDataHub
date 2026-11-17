//! Service composition; the common runner owns the only Application lifecycle.
#![forbid(unsafe_code)]
use datahub_app::{Composition, Result};
use datahub_config::{Command, Config, Service};

fn main() -> Result<()> {
    let command = datahub_config::command(std::env::args().skip(1))?;
    let (path, check) = match command {
        Command::Help => {
            println!("fedengine-server --config FILE [--check-config] | --version");
            return Ok(());
        }
        Command::Version => {
            println!(
                "fedengine-server {} / Modorch 0.0.3 / development",
                env!("CARGO_PKG_VERSION")
            );
            return Ok(());
        }
        Command::Run(path) => (path, false),
        Command::Check(path) => (path, true),
    };
    let config = Config::load(&path, Service::FedEngine)?;
    let mut composition = Composition::new(config.clone());
    let fed_plan = datahub_fedengine::Plan::load(&config)?;
    if let Some((slot, contrib)) = fed_plan.web_contribution() {
        let listen = match config.web.listen {
            Some(addr) => addr,
            None => fed_plan.management_listen()?,
        };
        fed_plan.register_with_router_slot(&mut composition, Some(slot))?;
        datahub_web::WebGatewayPlan::new(
            listen,
            config.web.bearer_token_env.clone(),
            config.application.name.clone(),
            vec![contrib],
        )?
        .register(&mut composition)?;
    } else {
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
