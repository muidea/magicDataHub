//! Real public Facades composed inside the host's sole Application.
use datahub_app::{Composition, Result};
use datahub_config::{Config, Service};
use modorch::{ComponentRef, Destination};
use modorch_fedengine::{
    FedEngineCancellation, FedEngineDataType, FedEnginePrincipal, FedEngineResultChunk,
    FedEngineValue,
};
use modorch_mqtt::{BrokerStatus, STATUS_TOPIC};
use std::{
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let path = std::env::temp_dir().join(format!(
            "mdh-host-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&path).unwrap();
        std::fs::create_dir_all(path.join("config/mqtt")).unwrap();
        std::fs::create_dir_all(path.join("config/fedengine")).unwrap();
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        for file in [
            "combined-development.toml",
            "mqtt/combined.toml",
            "fedengine/combined.toml",
        ] {
            let mut text = std::fs::read_to_string(root.join("config").join(file)).unwrap();
            text = text
                .replace(":2883\"", ":0\"")
                .replace(":25432\"", ":0\"")
                .replace(":25433\"", ":0\"");
            if file == "combined-development.toml" {
                text.push_str("pgwire = false\n");
            }
            std::fs::write(path.join("config").join(file), text).unwrap();
        }
        Self(path)
    }
    fn config(&self) -> Config {
        Config::load(
            &self.0.join("config/combined-development.toml"),
            Service::Combined,
        )
        .unwrap()
    }
    fn file(&self, name: &str, from: &str, to: &str) {
        let path = self.0.join("config").join(name);
        let text = std::fs::read_to_string(&path).unwrap();
        assert!(text.contains(from));
        std::fs::write(path, text.replace(from, to)).unwrap();
    }
    fn assemble(
        &self,
    ) -> (
        Composition,
        std::sync::Arc<modorch_mqtt::MqttRegisteredComponents>,
        std::sync::Arc<modorch_fedengine::FedEngineRegisteredComponents>,
    ) {
        let host = self.config();
        let mut c = Composition::new(host.clone());
        let m = datahub_mqtt::Plan::load(&host)
            .unwrap()
            .register(&mut c)
            .unwrap();
        let f = datahub_fedengine::Plan::load(&host)
            .unwrap()
            .register(&mut c)
            .unwrap();
        (c, m, f)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn registered_plan_has_no_io_and_combined_facades_share_ready_application() -> Result<()> {
    let fixture = Fixture::new();
    let config = fixture.config();
    let plan = datahub_fedengine::Plan::load(&config)?;
    assert_eq!(
        Path::new(&plan.config().duckdb.database_path),
        fixture
            .0
            .join("config/fedengine/../../var/combined/fedengine.duckdb")
    );
    assert_eq!(
        plan.config().users[0].password_secret_ref,
        "FEDENGINE_PASSWORD"
    );
    let (c, m, f) = fixture.assemble();
    c.check()?;
    let prepared = c.build()?;
    assert!(!fixture.0.join("var").exists());
    assert!(m.handle().local_addr().is_none());
    assert!(!f.handle().status().ready);
    let running = prepared.start().await?;
    assert_eq!(running.endpoints.len(), 2);
    assert_eq!(
        f.handle().status().application_id,
        Some(running.application().health().application_id)
    );
    let verification: Result<_> = async {
        let status = running
            .application()
            .event_hub()
            .external()
            .request::<_, BrokerStatus>(
                STATUS_TOPIC,
                Destination::Component(ComponentRef::module(m.module_ids().next().unwrap())),
                (),
                modorch::MessageOptions {
                    payload_bytes: Some(0),
                    ..Default::default()
                },
            )?
            .wait(Duration::from_secs(3))
            .await?;
        assert!(status.active);
        let q = f.query_client(
            running.application(),
            FedEnginePrincipal::authenticated("fedengine", "host-test/session")?,
            Default::default(),
        )?;
        let p = q
            .prepare(
                "fedengine",
                "SELECT $1::INTEGER",
                &[Some(FedEngineDataType::Int4)],
                &FedEngineCancellation::new(),
            )
            .await?;
        let mut stream = q
            .execute(
                &p,
                vec![FedEngineValue::I32(42)],
                &FedEngineCancellation::new(),
            )
            .await?;
        let mut found = false;
        let mut terminal = false;
        while let Some(chunk) = stream.next().await? {
            match chunk {
                FedEngineResultChunk::Rows(rows) => {
                    assert!(matches!(rows[0][0], FedEngineValue::I32(42)));
                    found = true;
                }
                FedEngineResultChunk::Complete(_) => terminal = true,
            }
        }
        assert!(found && terminal);
        Ok(q)
    }
    .await;
    let shutdown = running.shutdown().await;
    let q = verification?;
    shutdown?;
    assert_eq!(running.application().task_metrics().tasks, 0);
    assert_eq!(
        q.prepare("fedengine", "SELECT 1", &[], &FedEngineCancellation::new())
            .await
            .err()
            .unwrap()
            .sqlstate,
        "55000"
    );
    Ok(())
}
#[test]
fn budgets_and_colliding_ports_fail_before_any_runtime_resources() {
    let fixture = Fixture::new();
    fixture.file(
        "combined-development.toml",
        "shutdown_timeout_ms = 30000",
        "shutdown_timeout_ms = 30000\nmax_tasks = 1",
    );
    let (c, _, _) = fixture.assemble();
    assert!(c.check().is_err());
    assert!(!fixture.0.join("var").exists());
    let fixture = Fixture::new();
    fixture.file("mqtt/combined.toml", "127.0.0.1:0", "127.0.0.1:34567");
    fixture.file("fedengine/combined.toml", "127.0.0.1:0", "127.0.0.1:34567");
    let (c, _, _) = fixture.assemble();
    assert!(c.check().is_err());
    assert!(!fixture.0.join("var").exists());
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn two_applications_keep_files_hubs_and_capabilities_separate() -> Result<()> {
    let first = Fixture::new();
    let second = Fixture::new();
    let (a, ma, fa) = first.assemble();
    let a = a.build()?.start().await?;
    let (b, mb, fb) = second.assemble();
    let b = b.build()?.start().await?;
    assert_ne!(
        a.application().health().application_id,
        b.application().health().application_id
    );
    assert_ne!(ma.handle().local_addr(), mb.handle().local_addr());
    assert_eq!(
        fa.query_client(
            b.application(),
            FedEnginePrincipal::authenticated("fedengine", "foreign-test")?,
            Default::default()
        )
        .err()
        .unwrap()
        .sqlstate,
        "55000"
    );
    let before = fb.handle().status();
    a.shutdown().await?;
    assert!(b.ready() && mb.handle().status().active && fb.handle().status().ready);
    assert_eq!(before.application_id, fb.handle().status().application_id);
    b.shutdown().await?;
    Ok(())
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn occupied_database_listener_compensates_mqtt_and_preserves_reopenable_state() -> Result<()>
{
    let fixture = Fixture::new();
    let occupied = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let mqtt = std::net::TcpListener::bind("127.0.0.1:0")?;
    let mqtt_address = mqtt.local_addr()?;
    drop(mqtt);
    fixture.file(
        "mqtt/combined.toml",
        "127.0.0.1:0",
        &mqtt_address.to_string(),
    );
    fixture.file(
        "fedengine/combined.toml",
        "127.0.0.1:0",
        &occupied.local_addr()?.to_string(),
    );
    let (c, m, _) = fixture.assemble();
    assert!(c.build()?.start().await.is_err());
    assert!(!m.handle().status().active);
    drop(tokio::net::TcpListener::bind(mqtt_address).await?);
    drop(occupied);
    let (c, _, _) = fixture.assemble();
    let running = c.build()?.start().await?;
    running.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn unselected_business_libraries_create_no_files_listeners_or_components() -> Result<()> {
    for mqtt_only in [true, false] {
        let fixture = Fixture::new();
        let host = fixture.config();
        let mut c = Composition::new(host.clone());
        if mqtt_only {
            let f = modorch_fedengine::FedEngineBundle::new(
                datahub_fedengine::Plan::load(&host)?.config().clone(),
            )
            .profile(modorch_fedengine::FedEngineProfile::ManagedServer { pgwire: false })
            .register(c.builder())?;
            datahub_mqtt::Plan::load(&host)?.register(&mut c)?;
            let running = c.build()?.start().await?;
            let clean = !fixture.0.join("var").exists()
                && !f.handle().status().ready
                && f.components()
                    .all(|id| !running.application().health().components.contains_key(id));
            running.shutdown().await?;
            assert!(clean);
        } else {
            let m = modorch_mqtt::MqttBrokerBundle::new(
                datahub_mqtt::Plan::load(&host)?.config().clone(),
            )
            .register(c.builder())?;
            datahub_fedengine::Plan::load(&host)?.register(&mut c)?;
            let running = c.build()?.start().await?;
            let clean = m.handle().local_addr().is_none()
                && !m.handle().status().active
                && m.module_ids().all(|id| {
                    !running
                        .application()
                        .health()
                        .components
                        .contains_key(&ComponentRef::module(id))
                });
            running.shutdown().await?;
            assert!(clean);
        }
    }
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn observation_failure_preserves_cause_and_drains_started_libraries() -> Result<()> {
    let fixture = Fixture::new();
    let (mut c, m, f) = fixture.assemble();
    let captured = std::sync::Arc::new(std::sync::Mutex::new(None));
    let capture = captured.clone();
    c.observe(move |app| {
        *capture.lock().unwrap() = Some(app.clone());
        Err(datahub_app::invalid("injected host observer failure"))
    });
    let error = c.build()?.start().await.err().unwrap();
    assert!(error.to_string().contains("injected host observer failure"));
    let app = captured.lock().unwrap().take().unwrap();
    assert_eq!(app.task_metrics().tasks, 0);
    assert!(!m.handle().status().active && !f.handle().status().ready);
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn runner_retains_same_application_during_incomplete_http_drain() -> Result<()> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let fixture = Fixture::new();
    fixture.file(
        "combined-development.toml",
        "shutdown_timeout_ms = 30000",
        "shutdown_timeout_ms = 25\nshutdown_retry_ms = 5",
    );
    let (c, m, f) = fixture.assemble();
    let running = c.build()?.start().await?;
    let id = running.application().id();
    let address = f.handle().management_address().unwrap();
    let mut stream = tokio::net::TcpStream::connect(address).await?;
    let verification: Result<()> = async {
        stream.write_all(b"POST /api/v1/sources HTTP/1.1\r\nHost: localhost\r\nExpect: 100-continue\r\nContent-Length: 1000\r\n\r\n").await?;
        let mut interim = [0; 25];
        tokio::time::timeout(Duration::from_secs(5), stream.read_exact(&mut interim)).await??;
        assert_eq!(&interim, b"HTTP/1.1 100 Continue\r\n\r\n");
        Ok(())
    }.await;
    if let Err(error) = verification {
        drop(stream);
        running.shutdown().await?;
        return Err(error);
    }
    let stop = running.shutdown();
    tokio::pin!(stop);
    let observed = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            tokio::select! {
                result = &mut stop => return Err(result.err().unwrap_or_else(|| datahub_app::invalid("drain ended before retained HTTP body was released"))),
                _ = tokio::time::sleep(Duration::from_millis(1)) => {
                    if running.application().health().state == modorch::ApplicationState::StopIncomplete {
                        return Ok(());
                    }
                }
            }
        }
    }).await;
    let retained = running.application().task_metrics().tasks > 0
        && !running.ready()
        && !m.handle().status().active
        && f.handle().management_address().is_none();
    drop(stream);
    tokio::time::timeout(Duration::from_secs(10), &mut stop).await??;
    observed??;
    assert!(retained);
    assert_eq!(running.application().id(), id);
    assert_eq!(running.application().task_metrics().tasks, 0);
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn http_only_profile_ignores_unselected_pg_transport_and_auth_configuration() -> Result<()> {
    let fixture = Fixture::new();
    let path = fixture.0.join("config/fedengine/combined.toml");
    let text = std::fs::read_to_string(&path)?;
    let text = text
        .replacen(
            r#"listen = "127.0.0.1:0""#,
            r#"listen = "invalid-unselected-pg""#,
            1,
        )
        .replace(
            "tls_required = false",
            "tls_required = true\ntls_cert = ''\ntls_key = ''",
        )
        .replace(
            r#"password_secret_ref = "FEDENGINE_PASSWORD""#,
            "password_secret_ref = ''",
        );
    std::fs::write(path, text)?;
    let host = fixture.config();
    let mut c = Composition::new(host.clone());
    datahub_mqtt::Plan::load(&host)?.register(&mut c)?;
    datahub_fedengine::Plan::load(&host)?.register(&mut c)?;
    let running = c.build()?.start().await?;
    let valid = running.endpoints.len() == 2 && running.ready();
    running.shutdown().await?;
    assert!(valid);
    Ok(())
}

// Test-only public lifecycle probe. No debug/fault ingress is added to services.
#[derive(Clone, Default)]
struct HostSchedulerProbe(std::sync::Arc<std::sync::Mutex<Option<modorch::TaskScheduler>>>);
impl modorch::ModuleFactory for HostSchedulerProbe {
    fn descriptor(&self) -> modorch::ComponentDescriptor {
        modorch::ComponentDescriptor::new(ComponentRef::module("host-acceptance-probe"))
    }
    fn create(
        &self,
        _: &modorch::ComponentConfig,
    ) -> modorch::FrameworkResult<Box<dyn modorch::Module>> {
        Ok(Box::new(self.clone()))
    }
}
#[modorch::async_trait]
impl modorch::Lifecycle for HostSchedulerProbe {
    async fn setup(&mut self, ctx: &modorch::SetupContext) -> modorch::FrameworkResult<()> {
        *self.0.lock().unwrap() = Some(ctx.tasks.clone());
        Ok(())
    }
}
impl modorch::Module for HostSchedulerProbe {
    fn descriptor(&self) -> modorch::ComponentDescriptor {
        modorch::ComponentDescriptor::new(ComponentRef::module("host-acceptance-probe"))
    }
}
fn probe(composition: &mut Composition) -> HostSchedulerProbe {
    let probe = HostSchedulerProbe::default();
    composition
        .builder()
        .register_module(probe.clone())
        .unwrap();
    composition.enable_module("host-acceptance-probe");
    probe
}
async fn mqtt_status(
    running: &datahub_app::Running,
    m: &modorch_mqtt::MqttRegisteredComponents,
) -> Result<()> {
    let status = running
        .application()
        .event_hub()
        .external()
        .request::<_, BrokerStatus>(
            STATUS_TOPIC,
            Destination::Component(ComponentRef::module(m.module_ids().next().unwrap())),
            (),
            modorch::MessageOptions {
                payload_bytes: Some(0),
                ..Default::default()
            },
        )?
        .wait(Duration::from_secs(3))
        .await?;
    assert!(status.active);
    Ok(())
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn combined_failfast_drains_both_libraries_and_old_clients_stay_invalid() -> Result<()> {
    let fixture = Fixture::new();
    let (mut c, m, f) = fixture.assemble();
    let p = probe(&mut c);
    let running = c.build()?.start().await?;
    let id = running.application().id();
    let q = f.query_client(
        running.application(),
        FedEnginePrincipal::authenticated("fedengine", "host-fault-test")?,
        Default::default(),
    )?;
    let scheduler = p.0.lock().unwrap().clone().unwrap();
    let job = scheduler.spawn(|_| async {
        Err(modorch::FrameworkError::InvalidConfig(
            "injected host task fault".into(),
        ))
    })?;
    let failure =
        tokio::time::timeout(Duration::from_secs(5), running.application().wait()).await?;
    let shutdown = running.shutdown().await;
    assert!(failure.is_err());
    shutdown?;
    assert!(!m.handle().status().active && !f.handle().status().ready);
    assert_eq!(running.application().task_metrics().tasks, 0);
    assert!(job.wait_finished().await.is_err());
    assert!(scheduler.spawn(|_| async { Ok(()) }).is_err());
    assert_eq!(
        q.prepare("fedengine", "SELECT 1", &[], &FedEngineCancellation::new())
            .await
            .err()
            .unwrap()
            .sqlstate,
        "55000"
    );
    let (c, _, _) = fixture.assemble();
    let replacement = c.build()?.start().await?;
    assert_ne!(replacement.application().id(), id);
    let stale = q
        .prepare("fedengine", "SELECT 1", &[], &FedEngineCancellation::new())
        .await
        .err()
        .unwrap()
        .sqlstate;
    replacement.shutdown().await?;
    assert_eq!(stale, "55000");
    Ok(())
}

struct BlockingGate(std::sync::Arc<(std::sync::Mutex<bool>, std::sync::Condvar)>);
impl BlockingGate {
    fn release(&self) {
        *self.0.0.lock().unwrap() = true;
        self.0.1.notify_all();
    }
}
impl Drop for BlockingGate {
    fn drop(&mut self) {
        self.release();
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn combined_task_and_blocking_saturation_reject_without_starving_status_or_drain()
-> Result<()> {
    let fixture = Fixture::new();
    let (mut c, m, f) = fixture.assemble();
    let p = probe(&mut c);
    let limits = fixture.config().runtime;
    let running = c.build()?.start().await?;
    let scheduler = p.0.lock().unwrap().clone().unwrap();
    let gate = BlockingGate(std::sync::Arc::new((
        std::sync::Mutex::new(false),
        std::sync::Condvar::new(),
    )));
    let task_gate = std::sync::Arc::new(tokio::sync::Semaphore::new(0));
    let verification: Result<()> = async {
        let mut blocking = vec![];
        let mut rejected = false;
        for _ in 0..=limits.blocking_io_capacity {
            let wait_gate = gate.0.clone();
            match scheduler.spawn_blocking_io(move |_| {
                let mut released = wait_gate.0.lock().unwrap();
                while !*released {
                    released = wait_gate.1.wait(released).unwrap();
                }
                Ok(())
            }) {
                Ok(job) => blocking.push(job),
                Err(modorch::FrameworkError::Backpressure(_)) => {
                    rejected = true;
                    break;
                }
                Err(error) => {
                    gate.release();
                    return Err(error.into());
                }
            }
        }
        let control = mqtt_status(&running, &m).await;
        gate.release();
        for job in blocking {
            job.wait_finished().await?;
        }
        let mut tasks = vec![];
        let mut task_rejected = false;
        for _ in 0..=limits.max_tasks_per_owner {
            let wait = task_gate.clone();
            match scheduler.spawn(move |_| async move {
                wait.acquire().await.unwrap().forget();
                Ok(())
            }) {
                Ok(job) => tasks.push(job),
                Err(modorch::FrameworkError::Backpressure(_)) => {
                    task_rejected = true;
                    break;
                }
                Err(error) => {
                    task_gate.add_permits(tasks.len());
                    return Err(error.into());
                }
            }
        }
        let bounded = running.application().task_metrics().tasks <= limits.max_tasks;
        let task_control = mqtt_status(&running, &m).await;
        task_gate.add_permits(tasks.len());
        for job in tasks {
            job.wait_finished().await?;
        }
        let q = f.query_client(
            running.application(),
            FedEnginePrincipal::authenticated("fedengine", "quota-test")?,
            Default::default(),
        )?;
        let query = q
            .prepare("fedengine", "SELECT 42", &[], &FedEngineCancellation::new())
            .await;
        control?;
        task_control?;
        query?;
        assert!(rejected && task_rejected && bounded);
        Ok(())
    }
    .await;
    gate.release();
    task_gate.add_permits(limits.max_tasks_per_owner + 1);
    let shutdown = running.shutdown().await;
    verification?;
    shutdown?;
    assert_eq!(running.application().task_metrics().tasks, 0);
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "owned PG/MySQL fixture only; scripts/verify-federation.py creates and removes it"]
async fn combined_remote_clients_enforce_object_grants_and_shared_admission() -> Result<()> {
    let config_file = std::env::var_os("MDH_ACCEPTANCE_CONFIG").ok_or_else(|| {
        datahub_app::invalid("run scripts/verify-federation.py for owned source configuration")
    })?;
    let host = Config::load(Path::new(&config_file), Service::Combined)?;
    // The runner supplies its private, newly-created host directory. Never accept a business DSN here.
    let parent = Path::new(&config_file).parent().unwrap();
    if std::fs::read_to_string(parent.join("owned-fixture.marker"))?
        != "magicDataHub disposable sources\n"
    {
        return Err(datahub_app::invalid("owned fixture marker mismatch"));
    }
    let mut c = Composition::new(host.clone());
    let m = datahub_mqtt::Plan::load(&host)?.register(&mut c)?;
    let f = datahub_fedengine::Plan::load(&host)?.register(&mut c)?;
    let running = c.build()?.start().await?;
    let verification: Result<()> = async {
        let principal = || FedEnginePrincipal::authenticated("fedengine", "joint-fixture");
        let q = f.query_client(running.application(), principal()?, modorch_fedengine::FedEngineClientBudget {max_in_flight:1, ..Default::default()})?;
        let reader = f.query_client(running.application(), FedEnginePrincipal::authenticated("reader", "joint-reader")?, Default::default())?;
        let denied = reader.prepare("fedengine", "SELECT * FROM pg_crm.public.mdh_client_items", &[], &FedEngineCancellation::new()).await.err().unwrap();
        assert_eq!(denied.sqlstate, "42501");
        let denied_admin = f.management_client(running.application(), principal()?, Default::default(), Default::default())?;
        assert_eq!(denied_admin.source(modorch_fedengine::FedEngineSourceId("crm".into())).await.err().unwrap().sqlstate, "42501");
        let scoped = f.management_client(running.application(), principal()?, Default::default(), modorch_fedengine::FedEngineManagementGrant::default().source("crm"))?;
        assert_eq!(scoped.source(modorch_fedengine::FedEngineSourceId("crm".into())).await?.source.status, modorch_fedengine::FedEngineSourceStatus::Active);
        assert_eq!(scoped.source(modorch_fedengine::FedEngineSourceId("erp".into())).await.err().unwrap().sqlstate, "42501");
        assert_eq!(f.query_client(running.application(), FedEnginePrincipal::authenticated("unknown", "untrusted-name")?, Default::default()).err().unwrap().sqlstate, "42501");
        let p = q.prepare("pg_crm", "SELECT p.value+m.value AS total FROM pg_crm.public.mdh_client_items p JOIN mysql_erp.erp.mdh_client_items m ON p.id=m.id", &[], &FedEngineCancellation::new()).await?;
        let mut result = q.execute(&p, vec![], &FedEngineCancellation::new()).await?;
        let clone = q.clone();
        assert_eq!(clone.prepare("pg_crm", "SELECT 1", &[], &FedEngineCancellation::new()).await.err().unwrap().sqlstate, "53300");
        mqtt_status(&running, &m).await?;
        let mut row_seen = false;
        let mut terminal = false;
        while let Some(chunk) = result.next().await? {
            match chunk {
                FedEngineResultChunk::Rows(rows) => {assert!(matches!(rows[0][0], FedEngineValue::I32(40))); row_seen = true;},
                FedEngineResultChunk::Complete(_) => terminal = true,
            }
        }
        assert!(row_seen && terminal);
        drop(result);
        clone.prepare("pg_crm", "SELECT 1", &[], &FedEngineCancellation::new()).await?;
        mqtt_status(&running, &m).await?;
        Ok(())
    }.await;
    let shutdown = running.shutdown().await;
    verification?;
    shutdown?;
    assert_eq!(running.application().task_metrics().tasks, 0);
    assert_eq!(f.handle().metrics().active_workers, 0);
    Ok(())
}

#[derive(Clone)]
struct LateIngressFailure {
    mqtt: std::sync::Arc<modorch_mqtt::MqttRegisteredComponents>,
    http: std::net::SocketAddr,
    activated: std::sync::Arc<std::sync::atomic::AtomicBool>,
}
fn late_descriptor() -> modorch::ComponentDescriptor {
    let mut d =
        modorch::ComponentDescriptor::new(ComponentRef::module("host-late-ingress-failure"));
    d.requires = vec![
        ComponentRef::module("mqtt-broker"),
        ComponentRef::initiator("management-api"),
    ];
    d
}
impl modorch::ModuleFactory for LateIngressFailure {
    fn descriptor(&self) -> modorch::ComponentDescriptor {
        late_descriptor()
    }
    fn create(
        &self,
        _: &modorch::ComponentConfig,
    ) -> modorch::FrameworkResult<Box<dyn modorch::Module>> {
        Ok(Box::new(self.clone()))
    }
}
#[modorch::async_trait]
impl modorch::Lifecycle for LateIngressFailure {}
#[modorch::async_trait]
impl modorch::Module for LateIngressFailure {
    fn descriptor(&self) -> modorch::ComponentDescriptor {
        late_descriptor()
    }
    async fn activate_ingress(
        &mut self,
        _: &modorch::RuntimeContext,
    ) -> modorch::FrameworkResult<()> {
        let mqtt = self.mqtt.handle().local_addr().unwrap();
        assert!(self.mqtt.handle().status().active);
        let first = tokio::net::TcpStream::connect(mqtt).await.unwrap();
        let second = tokio::net::TcpStream::connect(self.http).await.unwrap();
        self.activated.store(true, Ordering::SeqCst);
        drop((first, second));
        Err(modorch::FrameworkError::InvalidConfig(
            "injected late ingress activation failure".into(),
        ))
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn late_activation_failure_compensates_two_already_live_ingresses() -> Result<()> {
    let fixture = Fixture::new();
    let reserved = std::net::TcpListener::bind("127.0.0.1:0")?;
    let http = reserved.local_addr()?;
    fixture.file("fedengine/combined.toml", "127.0.0.1:0", &http.to_string());
    drop(reserved);
    let (mut c, m, _) = fixture.assemble();
    let activated = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    c.builder().register_module(LateIngressFailure {
        mqtt: m.clone(),
        http,
        activated: activated.clone(),
    })?;
    c.enable_module("host-late-ingress-failure");
    let error = c.build()?.start().await.err().unwrap();
    assert!(activated.load(Ordering::SeqCst));
    assert!(
        error
            .to_string()
            .contains("injected late ingress activation failure")
    );
    let startup = error.downcast_ref::<modorch::StartupError>().unwrap();
    assert_eq!(startup.handle.task_metrics().tasks, 0);
    assert!(!m.handle().status().active);
    drop(std::net::TcpListener::bind(http)?);
    Ok(())
}
