//! One host-owned Application, bounded resources, observation and checked drain.
#![forbid(unsafe_code)]
use datahub_config::{Config, Runtime};
use modorch::{
    Application, ApplicationBuilder, ApplicationHandle, ComponentSelection, ExecutionResources,
    FrameworkError, HubLimits, ShutdownOptions, ShutdownReceipt,
};
use serde::Serialize;
use std::{io::Write, net::SocketAddr, time::Duration};

pub type Error = Box<dyn std::error::Error + Send + Sync>;
pub type Result<T> = std::result::Result<T, Error>;
pub fn invalid(message: &'static str) -> Error {
    std::io::Error::new(std::io::ErrorKind::InvalidInput, message).into()
}

/// Explicit advisory requirements; logical budgets are not RSS guarantees.
#[derive(Clone, Copy, Debug, Default, Serialize)]
pub struct Demand {
    pub tasks: usize,
    pub timers: usize,
    pub owner_tasks: usize,
    pub blocking_threads: usize,
    pub blocking_capacity: usize,
}
impl Demand {
    fn merge(self, other: Self) -> Result<Self> {
        let add = |a: usize, b: usize| {
            a.checked_add(b)
                .ok_or_else(|| invalid("combined resource demand overflow"))
        };
        Ok(Self {
            tasks: add(self.tasks, other.tasks)?,
            timers: add(self.timers, other.timers)?,
            owner_tasks: self.owner_tasks.max(other.owner_tasks),
            blocking_threads: add(self.blocking_threads, other.blocking_threads)?,
            blocking_capacity: add(self.blocking_capacity, other.blocking_capacity)?,
        })
    }
    pub fn validate(self, r: &Runtime) -> Result<()> {
        r.validate()?;
        if self.tasks > r.max_tasks
            || self.timers > r.max_timers
            || self.owner_tasks > r.max_tasks_per_owner
            || self.blocking_threads > r.blocking_io_threads
            || self.blocking_capacity > r.blocking_io_capacity
        {
            return Err(invalid(
                "host resource limits are below the declared business demand",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize)]
pub struct Endpoint {
    pub protocol: &'static str,
    pub address: SocketAddr,
}
/// Conservative conflict check; binding remains the final authority.
pub fn validate_endpoints(endpoints: &[Endpoint]) -> Result<()> {
    for (i, a) in endpoints.iter().enumerate() {
        if a.address.port() == 0 {
            continue;
        }
        for b in &endpoints[..i] {
            if a.address.port() == b.address.port()
                && (a.address.ip() == b.address.ip()
                    || a.address.ip().is_unspecified()
                    || b.address.ip().is_unspecified())
            {
                return Err(invalid("configured listener addresses overlap"));
            }
        }
    }
    Ok(())
}
type Observer = Box<dyn Fn(&ApplicationHandle) -> Result<Vec<Endpoint>> + Send + Sync>;

pub struct Composition {
    builder: ApplicationBuilder,
    selection: ComponentSelection,
    config: Config,
    demand: Demand,
    endpoints: Vec<Endpoint>,
    observers: Vec<Observer>,
}
impl Composition {
    pub fn new(config: Config) -> Self {
        Self {
            builder: ApplicationBuilder::new(&config.application.name),
            selection: ComponentSelection::new(),
            config,
            demand: Demand {
                tasks: 8,
                timers: 1,
                ..Default::default()
            },
            endpoints: vec![],
            observers: vec![],
        }
    }
    pub fn builder(&mut self) -> &mut ApplicationBuilder {
        &mut self.builder
    }
    pub fn enable_module(&mut self, id: &str) {
        self.selection.enable_module(id);
    }
    pub fn enable_initiator(&mut self, id: &str) {
        self.selection.enable_initiator(id);
    }
    pub fn require(&mut self, demand: Demand) -> Result<()> {
        self.demand = self.demand.merge(demand)?;
        Ok(())
    }
    pub fn endpoint(&mut self, protocol: &'static str, address: SocketAddr) {
        self.endpoints.push(Endpoint { protocol, address });
    }
    pub fn observe(
        &mut self,
        observer: impl Fn(&ApplicationHandle) -> Result<Vec<Endpoint>> + Send + Sync + 'static,
    ) {
        self.observers.push(Box::new(observer));
    }
    /// Pure planning check after registration. Does not build or start the App.
    pub fn check(&self) -> Result<Demand> {
        self.demand.validate(&self.config.runtime)?;
        validate_endpoints(&self.endpoints)?;
        Ok(self.demand)
    }
    pub fn build(self) -> Result<Prepared> {
        self.check()?;
        let r = &self.config.runtime;
        let app = self
            .builder
            .selection(self.selection)
            .task_limits(r.max_tasks, r.max_timers)
            .queue_capacities(r.queue_capacity, r.queue_capacity)
            .execution_resources(ExecutionResources {
                cpu_threads: r.cpu_threads,
                cpu_capacity: r.cpu_capacity,
                blocking_io_threads: r.blocking_io_threads,
                blocking_io_capacity: r.blocking_io_capacity,
                max_tasks_per_owner: r.max_tasks_per_owner,
                max_waiters: r.max_waiters,
                max_waiters_per_owner: r.max_waiters_per_owner,
                ..Default::default()
            })
            .message_limits(HubLimits {
                max_waiters: r.hub_max_waiters,
                max_waiters_per_target: r.hub_max_waiters_per_target,
                max_waiting_bytes: Some(r.hub_max_waiting_bytes),
                max_in_flight_bytes: Some(r.hub_max_in_flight_bytes),
                max_in_flight_bytes_per_target: Some(r.hub_max_in_flight_bytes_per_target),
                max_message_bytes: Some(r.hub_max_message_bytes),
                max_topic_bytes: 65535,
                ..Default::default()
            })
            .build()?;
        Ok(Prepared {
            app,
            observers: self.observers,
            runtime: self.config.runtime,
        })
    }
}
pub struct Prepared {
    app: Application,
    observers: Vec<Observer>,
    runtime: Runtime,
}
pub struct Running {
    app: ApplicationHandle,
    pub endpoints: Vec<Endpoint>,
    runtime: Runtime,
}
impl Prepared {
    /// Callers must retain the runtime through start and all cleanup retries.
    pub async fn start(self) -> Result<Running> {
        let app = match self.app.start().await {
            Ok(app) => app,
            Err(error) => {
                tracing::error!(cause=%error.source,cleanup=?error.cleanup,"application startup failed");
                if error.cleanup.is_err()
                    && let Err(cleanup) = drain(&error.handle, &self.runtime).await
                {
                    tracing::error!(%cleanup, "startup cleanup retry failed; preserving startup cause");
                }
                return Err(Box::new(error));
            }
        };
        let observed = (|| {
            if !app.health().ready {
                return Err(invalid("application not Ready after start"));
            }
            let mut endpoints = vec![];
            for observe in self.observers {
                endpoints.extend(observe(&app)?);
            }
            Ok(endpoints)
        })();
        match observed {
            Ok(endpoints) => Ok(Running {
                app,
                endpoints,
                runtime: self.runtime,
            }),
            Err(error) => {
                if let Err(cleanup) = drain(&app, &self.runtime).await {
                    tracing::error!(%cleanup, "observation cleanup failed; preserving observation cause");
                }
                Err(error)
            }
        }
    }
}
impl Running {
    pub fn application(&self) -> &ApplicationHandle {
        &self.app
    }
    pub fn ready(&self) -> bool {
        self.app.health().ready
    }
    pub async fn shutdown(&self) -> Result<()> {
        drain(&self.app, &self.runtime).await
    }
}
/// StopIncomplete is retried with the same Handle and live Runtime, never forced.
pub async fn drain(app: &ApplicationHandle, runtime: &Runtime) -> Result<()> {
    loop {
        match app
            .shutdown_checked(ShutdownOptions {
                timeout: Duration::from_millis(runtime.shutdown_timeout_ms),
            })
            .await
        {
            Ok(ShutdownReceipt::GracefullyStopped) => {
                tracing::info!("checked shutdown: GracefullyStopped");
                return Ok(());
            }
            Ok(ShutdownReceipt::Forced { .. }) => {
                return Err(invalid("Forced shutdown did not prove resource release"));
            }
            Err(error @ FrameworkError::StopIncomplete { .. }) => {
                tracing::warn!(%error,"StopIncomplete; retaining Application/Runtime and retrying");
                tokio::time::sleep(Duration::from_millis(runtime.shutdown_retry_ms)).await;
            }
            Err(error) => return Err(Box::new(error)),
        }
    }
}
#[cfg(unix)]
struct Signals {
    interrupt: tokio::signal::unix::Signal,
    terminate: tokio::signal::unix::Signal,
}
#[cfg(unix)]
impl Signals {
    fn install() -> std::io::Result<Self> {
        use tokio::signal::unix::{SignalKind, signal};
        Ok(Self {
            interrupt: signal(SignalKind::interrupt())?,
            terminate: signal(SignalKind::terminate())?,
        })
    }
    async fn wait(&mut self) -> std::io::Result<()> {
        let result = tokio::select! {r=self.interrupt.recv()=>r,r=self.terminate.recv()=>r};
        result.ok_or_else(|| std::io::Error::other("signal stream closed"))
    }
}
/// Linux/Unix service runner. Signal subscriptions exist before startup starts.
#[cfg(unix)]
pub async fn serve(prepared: Prepared) -> Result<()> {
    let mut signals = Signals::install()?;
    let running = prepared.start().await?;
    let ready = lifecycle_event(
        serde_json::json!({"event":"Ready","application":running.app.health().name,"application_id":format!("{:?}",running.app.health().application_id),"endpoints":running.endpoints}),
    );
    if let Err(error) = ready {
        let cleanup = running.shutdown().await;
        tracing::error!(?cleanup, "Ready output failed; application drained");
        return Err(error);
    }
    let result: Result<()> = tokio::select! {
        r=signals.wait()=>r.map_err(Into::into),
        r=running.app.wait()=>r.map_err(Into::into),
    };
    // Retain a recorded failure even if a signal won the race with app.wait().
    let failure = running.app.health().failure;
    let stopping = lifecycle_event(serde_json::json!({"event":"Stopping"}));
    let shutdown = running.shutdown().await;
    result?;
    shutdown?;
    stopping?;
    if let Some(error) = failure.or(running.app.health().failure) {
        return Err(Box::new(error));
    }
    Ok(())
}
#[cfg(not(unix))]
pub async fn serve(_: Prepared) -> Result<()> {
    Err(invalid("this service runner currently supports Unix only"))
}
fn lifecycle_event(value: serde_json::Value) -> Result<()> {
    writeln!(std::io::stdout().lock(), "{value}")?;
    Ok(())
}
pub fn init_logging() -> Result<()> {
    let filter =
        tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into());
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_ansi(false)
        .try_init()
        .map_err(|_| invalid("cannot install host logging subscriber"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn resource_merge_rejects_overflow_and_uses_owner_maximum() {
        let a = Demand {
            tasks: 20,
            owner_tasks: 10,
            blocking_threads: 2,
            ..Default::default()
        };
        let b = Demand {
            tasks: 30,
            owner_tasks: 15,
            blocking_threads: 3,
            ..Default::default()
        };
        let merged = a.merge(b).unwrap();
        assert_eq!(
            (merged.tasks, merged.owner_tasks, merged.blocking_threads),
            (50, 15, 5)
        );
        assert!(
            Demand {
                tasks: usize::MAX,
                ..a
            }
            .merge(b)
            .is_err()
        );
        assert!(
            a.validate(&Runtime {
                max_tasks: 1,
                ..Default::default()
            })
            .is_err()
        );
    }
    #[test]
    fn listener_conflicts_include_wildcards_but_allow_ephemeral_ports() {
        let pair = |a: &str, b: &str| {
            vec![
                Endpoint {
                    protocol: "mqtt",
                    address: a.parse().unwrap(),
                },
                Endpoint {
                    protocol: "pgwire",
                    address: b.parse().unwrap(),
                },
            ]
        };
        assert!(validate_endpoints(&pair("127.0.0.1:1883", "127.0.0.1:1883")).is_err());
        assert!(validate_endpoints(&pair("0.0.0.0:1883", "127.0.0.1:1883")).is_err());
        assert!(validate_endpoints(&pair("[::]:1883", "127.0.0.1:1883")).is_err());
        validate_endpoints(&pair("127.0.0.1:0", "127.0.0.1:0")).unwrap();
    }
}
