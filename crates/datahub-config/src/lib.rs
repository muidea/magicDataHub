//! Host configuration only; no business library, listener or secret access.
#![forbid(unsafe_code)]
use serde::Deserialize;
use std::{
    io::Read,
    path::{Path, PathBuf},
};

pub const MAX_CONFIG_BYTES: usize = 1024 * 1024;
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Invalid(&'static str),
    #[error("configuration file I/O failed: {0}")]
    Io(#[from] std::io::Error),
}
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Service {
    Mqtt,
    FedEngine,
    Combined,
}
impl Service {
    pub fn binary(self) -> &'static str {
        match self {
            Self::Mqtt => "mqtt-server",
            Self::FedEngine => "fedengine-server",
            Self::Combined => "datahub-server",
        }
    }
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Application {
    pub name: String,
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileReference {
    pub config_file: PathBuf,
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FedEngineReference {
    pub config_file: PathBuf,
    #[serde(default = "yes")]
    pub pgwire: bool,
    #[serde(default = "yes")]
    pub management_api: bool,
}
fn yes() -> bool {
    true
}

#[derive(Clone, Debug, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Runtime {
    pub max_tasks: usize,
    pub max_timers: usize,
    pub max_tasks_per_owner: usize,
    pub async_threads: usize,
    pub tokio_blocking_threads: usize,
    pub cpu_threads: usize,
    pub cpu_capacity: usize,
    pub blocking_io_threads: usize,
    pub blocking_io_capacity: usize,
    pub max_waiters: usize,
    pub max_waiters_per_owner: usize,
    pub hub_max_waiters: usize,
    pub hub_max_waiters_per_target: usize,
    pub hub_max_message_bytes: usize,
    pub hub_max_in_flight_bytes: usize,
    pub hub_max_in_flight_bytes_per_target: usize,
    pub hub_max_waiting_bytes: usize,
    pub queue_capacity: usize,
    pub shutdown_timeout_ms: u64,
    pub shutdown_retry_ms: u64,
}
impl Default for Runtime {
    fn default() -> Self {
        Self {
            max_tasks: 2048,
            max_timers: 64,
            max_tasks_per_owner: 512,
            async_threads: 2,
            tokio_blocking_threads: 8,
            cpu_threads: 2,
            cpu_capacity: 8,
            blocking_io_threads: 8,
            blocking_io_capacity: 128,
            max_waiters: 512,
            max_waiters_per_owner: 128,
            hub_max_waiters: 256,
            hub_max_waiters_per_target: 64,
            hub_max_message_bytes: 1024 * 1024,
            hub_max_in_flight_bytes: 16 * 1024 * 1024,
            hub_max_in_flight_bytes_per_target: 8 * 1024 * 1024,
            hub_max_waiting_bytes: 8 * 1024 * 1024,
            queue_capacity: 64,
            shutdown_timeout_ms: 30000,
            shutdown_retry_ms: 100,
        }
    }
}
impl Runtime {
    pub fn validate(&self) -> Result<()> {
        let bounded = [
            self.max_tasks,
            self.max_timers,
            self.max_tasks_per_owner,
            self.cpu_capacity,
            self.blocking_io_capacity,
            self.max_waiters,
            self.max_waiters_per_owner,
            self.hub_max_waiters,
            self.hub_max_waiters_per_target,
            self.queue_capacity,
        ];
        if bounded.iter().any(|n| *n == 0 || *n > 1_000_000)
            || !(1..=4096).contains(&self.async_threads)
            || !(1..=4096).contains(&self.tokio_blocking_threads)
            || !(1..=4096).contains(&self.cpu_threads)
            || !(1..=4096).contains(&self.blocking_io_threads)
            || self.cpu_capacity < self.cpu_threads
            || self.blocking_io_capacity < self.blocking_io_threads
            || self.max_waiters_per_owner > self.max_waiters
            || self.hub_max_waiters_per_target > self.hub_max_waiters
            || !(1..=3600000).contains(&self.shutdown_timeout_ms)
            || !(1..=10000).contains(&self.shutdown_retry_ms)
            || [
                self.hub_max_message_bytes,
                self.hub_max_in_flight_bytes,
                self.hub_max_in_flight_bytes_per_target,
                self.hub_max_waiting_bytes,
            ]
            .iter()
            .any(|n| *n == 0 || *n > 1024 * 1024 * 1024)
            || self.hub_max_message_bytes > self.hub_max_in_flight_bytes_per_target
            || self.hub_max_in_flight_bytes_per_target > self.hub_max_in_flight_bytes
        {
            return Err(Error::Invalid("invalid host resource/shutdown bounds"));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub application: Application,
    #[serde(default)]
    pub runtime: Runtime,
    pub mqtt: Option<FileReference>,
    pub fedengine: Option<FedEngineReference>,
}
impl Config {
    /// File references use the canonical host configuration's parent, never library CWD.
    pub fn load(path: &Path, service: Service) -> Result<Self> {
        let path = path.canonicalize()?;
        let input = read_document(&path, MAX_CONFIG_BYTES)?;
        let mut config: Self = toml::from_str(&input)
            .map_err(|_| Error::Invalid("invalid host TOML (unknown field or invalid value)"))?;
        config.validate(service)?;
        let base = path
            .parent()
            .ok_or(Error::Invalid("configuration has no parent"))?;
        if let Some(m) = &mut config.mqtt {
            m.config_file = resolve(base, &m.config_file)?;
        }
        if let Some(f) = &mut config.fedengine {
            f.config_file = resolve(base, &f.config_file)?;
        }
        Ok(config)
    }
    pub fn validate(&self, service: Service) -> Result<()> {
        if self.application.name.is_empty()
            || self.application.name.len() > 128
            || self.application.name.chars().any(char::is_control)
        {
            return Err(Error::Invalid("invalid application name"));
        }
        let wanted = match service {
            Service::Mqtt => (true, false),
            Service::FedEngine => (false, true),
            Service::Combined => (true, true),
        };
        if (self.mqtt.is_some(), self.fedengine.is_some()) != wanted {
            return Err(Error::Invalid(
                "service requires exactly its declared business libraries",
            ));
        }
        if self
            .fedengine
            .as_ref()
            .is_some_and(|f| !f.pgwire && !f.management_api)
        {
            return Err(Error::Invalid(
                "EmbeddedCore is not supported; select PGWire or management HTTP",
            ));
        }
        self.runtime.validate()
    }
}
pub fn resolve(base: &Path, value: &Path) -> Result<PathBuf> {
    if !base.is_absolute() || value.as_os_str().is_empty() {
        return Err(Error::Invalid(
            "empty path or non-absolute configuration base",
        ));
    }
    Ok(if value.is_absolute() {
        value.to_owned()
    } else {
        base.join(value)
    })
}
/// Read a bounded configuration document, without reading any referenced secret.
pub fn read_document(path: &Path, limit: usize) -> Result<String> {
    let mut bytes = Vec::new();
    std::fs::File::open(path)?
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        return Err(Error::Invalid("configuration size limit exceeded"));
    }
    String::from_utf8(bytes).map_err(|_| Error::Invalid("configuration must be UTF-8"))
}

pub enum Command {
    Run(PathBuf),
    Check(PathBuf),
    Help,
    Version,
}
pub fn command(args: impl IntoIterator<Item = String>) -> Result<Command> {
    let mut path = None;
    let mut check = false;
    let mut args = args.into_iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--config" if path.is_none() => {
                path = Some(PathBuf::from(
                    args.next()
                        .ok_or(Error::Invalid("--config requires a path"))?,
                ));
            }
            "--check-config" if !check => check = true,
            "--help" | "-h" if path.is_none() && !check => {
                if args.next().is_none() {
                    return Ok(Command::Help);
                }
                return Err(Error::Invalid("--help must be used alone"));
            }
            "--version" | "-V" if path.is_none() && !check => {
                if args.next().is_none() {
                    return Ok(Command::Version);
                }
                return Err(Error::Invalid("--version must be used alone"));
            }
            _ => return Err(Error::Invalid("unknown or duplicate CLI argument")),
        }
    }
    let path = path.ok_or(Error::Invalid("--config is required"))?;
    Ok(if check {
        Command::Check(path)
    } else {
        Command::Run(path)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Config {
        Config {
            application: Application {
                name: "test".into(),
            },
            runtime: Default::default(),
            mqtt: Some(FileReference {
                config_file: "mqtt.toml".into(),
            }),
            fedengine: None,
        }
    }
    #[test]
    fn fixed_service_identity_unknown_fields_and_cli_are_strict() {
        let c = config();
        c.validate(Service::Mqtt).unwrap();
        assert!(c.validate(Service::Combined).is_err());
        assert!(
            toml::from_str::<Config>("[application]\nname='test'\npassword='private-canary'\n")
                .is_err()
        );
        for args in [
            vec![],
            vec!["--config"],
            vec!["--config", "a", "--config", "b"],
            vec!["--check-config"],
            vec!["--version", "--config", "a"],
        ] {
            assert!(command(args.into_iter().map(str::to_owned)).is_err());
        }
        assert!(matches!(
            command(["--config", "a", "--check-config"].map(str::to_owned)).unwrap(),
            Command::Check(_)
        ));
    }
    #[test]
    fn resource_boundaries_reject_zero_excess_and_inconsistent_limits() {
        let original = Runtime::default();
        original.validate().unwrap();
        for r in [
            Runtime {
                max_tasks: 0,
                ..original.clone()
            },
            Runtime {
                cpu_capacity: 1,
                ..original.clone()
            },
            Runtime {
                hub_max_message_bytes: usize::MAX,
                ..original.clone()
            },
            Runtime {
                max_waiters_per_owner: 1024,
                ..original.clone()
            },
            Runtime {
                shutdown_timeout_ms: u64::MAX,
                ..original.clone()
            },
        ] {
            assert!(r.validate().is_err());
        }
        assert!(resolve(Path::new("relative"), Path::new("x")).is_err());
        assert_eq!(
            resolve(Path::new("/config"), Path::new("lib/one.toml")).unwrap(),
            Path::new("/config/lib/one.toml")
        );
    }
}
