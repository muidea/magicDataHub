//! End-to-end verification of standalone MQTT Web management and combined DataHub Web management.
use datahub_app::{Composition, Result};
use datahub_config::{Config, Service};
use datahub_web::WebGatewayPlan;
use std::{
    path::Path,
    sync::atomic::{AtomicU64, Ordering},
};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;

struct TempConfig(std::path::PathBuf);
impl TempConfig {
    fn new(combined: bool, auth_env: Option<&str>) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let dir = std::env::temp_dir().join(format!(
            "mdh-web-test-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(dir.join("config/mqtt")).unwrap();
        std::fs::create_dir_all(dir.join("config/fedengine")).unwrap();
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        if combined {
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
                    text.push_str("\n[web]\nenabled = true\n");
                    if let Some(env_name) = auth_env {
                        text.push_str(&format!("bearer_token_env = \"{env_name}\"\n"));
                    }
                }
                std::fs::write(dir.join("config").join(file), text).unwrap();
            }
        } else {
            let mut host =
                std::fs::read_to_string(root.join("config/mqtt-development.toml")).unwrap();
            host.push_str("\n[web]\nenabled = true\nlisten = \"127.0.0.1:0\"\n");
            if let Some(env_name) = auth_env {
                host.push_str(&format!("bearer_token_env = \"{env_name}\"\n"));
            }
            std::fs::write(dir.join("config/mqtt-development.toml"), host).unwrap();
            let mqtt = std::fs::read_to_string(root.join("config/mqtt/standalone.toml"))
                .unwrap()
                .replace(":1883\"", ":0\"");
            std::fs::write(dir.join("config/mqtt/standalone.toml"), mqtt).unwrap();
        }
        Self(dir)
    }
    fn path(&self, name: &str) -> std::path::PathBuf {
        self.0.join("config").join(name)
    }
}
impl Drop for TempConfig {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

async fn http_call(
    addr: std::net::SocketAddr,
    method: &str,
    path: &str,
    token: Option<&str>,
    body: Option<&str>,
) -> (u16, String) {
    let mut stream = TcpStream::connect(addr).await.unwrap();
    let payload = body.unwrap_or("");
    let mut req = format!("{method} {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n");
    if let Some(t) = token {
        req.push_str(&format!("Authorization: Bearer {t}\r\n"));
    }
    if body.is_some() {
        req.push_str(&format!(
            "Content-Type: application/json\r\nContent-Length: {}\r\n",
            payload.len()
        ));
    }
    req.push_str("\r\n");
    req.push_str(payload);
    stream.write_all(req.as_bytes()).await.unwrap();
    let mut reader = BufReader::new(stream);
    let mut status_line = String::new();
    reader.read_line(&mut status_line).await.unwrap();
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    let mut len = None;
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).await.unwrap();
        if line == "\r\n" || line.is_empty() {
            break;
        }
        if let Some((k, v)) = line.split_once(':')
            && k.eq_ignore_ascii_case("content-length")
        {
            len = Some(v.trim().parse::<usize>().unwrap());
        }
    }
    let mut buf = Vec::new();
    if let Some(n) = len {
        buf.resize(n, 0);
        reader.read_exact(&mut buf).await.unwrap();
    } else {
        reader.read_to_end(&mut buf).await.unwrap();
    }
    (status, String::from_utf8_lossy(&buf).into_owned())
}

fn mqtt_connect_packet(client_id: &str) -> Vec<u8> {
    let cid = client_id.as_bytes();
    let mut body = vec![0, 4, b'M', b'Q', b'T', b'T', 4, 2, 0, 30];
    body.extend_from_slice(&(cid.len() as u16).to_be_bytes());
    body.extend_from_slice(cid);
    let mut frame = vec![0x10, body.len() as u8];
    frame.extend_from_slice(&body);
    frame
}

fn mqtt_subscribe_packet(topic: &str) -> Vec<u8> {
    let t = topic.as_bytes();
    let mut body = vec![0, 1];
    body.extend_from_slice(&(t.len() as u16).to_be_bytes());
    body.extend_from_slice(t);
    body.push(1);
    let mut frame = vec![0x82, body.len() as u8];
    frame.extend_from_slice(&body);
    frame
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn standalone_mqtt_web_ui_tracks_clients_takeovers_events_and_enforces_token() -> Result<()> {
    let env_key = "MDH_TEST_MQTT_WEB_TOKEN";
    let token = "test-web-token-0123456789abcdef";
    unsafe {
        std::env::set_var(env_key, token);
    }
    let fixture = TempConfig::new(false, Some(env_key));
    let config = Config::load(&fixture.path("mqtt-development.toml"), Service::Mqtt)?;
    let mut composition = Composition::new(config.clone());
    let mut mqtt_plan = datahub_mqtt::Plan::load(&config)?;
    let contrib = mqtt_plan.enable_web_admin();
    mqtt_plan.register(&mut composition)?;
    WebGatewayPlan::new(
        config.web.listen.unwrap(),
        config.web.bearer_token_env.clone(),
        config.application.name.clone(),
        vec![contrib],
    )?
    .register(&mut composition)?;

    let running = composition.build()?.start().await?;
    let mqtt_addr = running
        .endpoints
        .iter()
        .find(|e| e.protocol == "mqtt")
        .unwrap()
        .address;
    let web_addr = running
        .endpoints
        .iter()
        .find(|e| e.protocol == "management")
        .unwrap()
        .address;

    let (status, caps) = http_call(web_addr, "GET", "/api/v1/capabilities", None, None).await;
    assert_eq!(status, 200);
    let caps: serde_json::Value = serde_json::from_str(&caps)?;
    assert_eq!(caps["services"], serde_json::json!(["mqtt"]));
    assert_eq!(caps["auth_required"], true);

    let (status, _) = http_call(web_addr, "GET", "/api/v1/mqtt/stats", None, None).await;
    assert_eq!(status, 401);

    let (status, js) = http_call(web_addr, "GET", "/manage/mqtt.js", None, None).await;
    assert_eq!(status, 200);
    assert!(js.contains("mqtt-clients"));

    // Connect first MQTT client and subscribe.
    let mut c1 = TcpStream::connect(mqtt_addr).await?;
    c1.write_all(&mqtt_connect_packet("dup-client")).await?;
    let mut ack = [0u8; 4];
    c1.read_exact(&mut ack).await?;
    assert_eq!(ack, [0x20, 2, 0, 0]);
    c1.write_all(&mqtt_subscribe_packet("sensors/temp")).await?;
    let mut suback = [0u8; 5];
    c1.read_exact(&mut suback).await?;
    assert_eq!(suback[0], 0x90);

    let (status, clients_json) = http_call(
        web_addr,
        "GET",
        "/api/v1/mqtt/clients?limit=20",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    let clients: serde_json::Value = serde_json::from_str(&clients_json)?;
    assert_eq!(clients["items"].as_array().unwrap().len(), 1);
    let first_conn = clients["items"][0]["connection_id"].as_u64().unwrap();
    assert_eq!(clients["items"][0]["client_id"], "dup-client");
    assert_eq!(clients["items"][0]["online"], true);
    assert!(clients["items"][0]["peer_addr"].as_str().is_some());

    let (status, subs_json) = http_call(
        web_addr,
        "GET",
        "/api/v1/mqtt/subscriptions?client_id=dup-client",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    let subs: serde_json::Value = serde_json::from_str(&subs_json)?;
    assert_eq!(subs["items"][0]["filter"], "sensors/temp");

    // Verify Topic filters, match simulator, admin publish, active topics & retained lifecycle while c1 is subscribed.
    let (status, filters_json) = http_call(
        web_addr,
        "GET",
        "/api/v1/mqtt/topics/filters",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    let filters: serde_json::Value = serde_json::from_str(&filters_json)?;
    assert_eq!(filters["items"][0]["filter"], "sensors/temp");

    let (status, match_json) = http_call(
        web_addr,
        "GET",
        "/api/v1/mqtt/topics/match?topic=sensors%2Ftemp",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    let matched: serde_json::Value = serde_json::from_str(&match_json)?;
    assert_eq!(matched["matches"][0]["client_id"], "dup-client");

    let (status, pub_json) = http_call(
        web_addr,
        "POST",
        "/api/v1/mqtt/publish",
        Some(token),
        Some("{\"topic\":\"sensors/temp\",\"payload\":\"42.5\",\"qos\":2,\"retain\":true}"),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&pub_json)?["matched_sessions"],
        1
    );

    // Second client with same ClientID triggers takeover.
    let mut c2 = TcpStream::connect(mqtt_addr).await?;
    c2.write_all(&mqtt_connect_packet("dup-client")).await?;
    c2.read_exact(&mut ack).await?;
    assert_eq!(ack, [0x20, 2, 0, 0]);

    let (status, clients2) =
        http_call(web_addr, "GET", "/api/v1/mqtt/clients", Some(token), None).await;
    assert_eq!(status, 200);
    let clients2: serde_json::Value = serde_json::from_str(&clients2)?;
    let second_conn = clients2["items"][0]["connection_id"].as_u64().unwrap();
    assert_ne!(first_conn, second_conn);

    // Stale connection_id kick is fenced (returns false); current connection_id succeeds.
    let (status, stale) = http_call(
        web_addr,
        "POST",
        "/api/v1/mqtt/disconnect",
        Some(token),
        Some(&format!(
            "{{\"client_id\":\"dup-client\",\"connection_id\":{first_conn}}}"
        )),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&stale)?["disconnected"],
        false
    );

    let (status, live) = http_call(
        web_addr,
        "POST",
        "/api/v1/mqtt/disconnect",
        Some(token),
        Some(&format!(
            "{{\"client_id\":\"dup-client\",\"connection_id\":{second_conn}}}"
        )),
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&live)?["disconnected"],
        true
    );

    let (status, active_json) = http_call(
        web_addr,
        "GET",
        "/api/v1/mqtt/topics/active",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    let active: serde_json::Value = serde_json::from_str(&active_json)?;
    assert_eq!(active["items"][0]["topic"], "sensors/temp");

    let (status, retained_json) =
        http_call(web_addr, "GET", "/api/v1/mqtt/retained", Some(token), None).await;
    assert_eq!(status, 200);
    let retained: serde_json::Value = serde_json::from_str(&retained_json)?;
    assert_eq!(retained["items"][0]["payload_preview"], "42.5");

    let (status, detail_json) = http_call(
        web_addr,
        "GET",
        "/api/v1/mqtt/retained/detail?topic=sensors%2Ftemp",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    let detail: serde_json::Value = serde_json::from_str(&detail_json)?;
    assert_eq!(detail["item"]["payload_bytes"], 4);

    let (status, del_json) = http_call(
        web_addr,
        "DELETE",
        "/api/v1/mqtt/retained?topic=sensors%2Ftemp",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&del_json)?["deleted"],
        true
    );

    let (status, events_json) = http_call(
        web_addr,
        "GET",
        "/api/v1/mqtt/events?limit=20",
        Some(token),
        None,
    )
    .await;
    assert_eq!(status, 200);
    let events: serde_json::Value = serde_json::from_str(&events_json)?;
    let kinds: Vec<&str> = events["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["kind"].as_str().unwrap())
        .collect();
    assert!(kinds.contains(&"taken_over"));
    assert!(kinds.contains(&"kicked"));
    assert!(kinds.contains(&"admin_publish"));
    assert!(kinds.contains(&"retained_deleted"));

    drop((c1, c2));
    running.shutdown().await?;
    Ok(())
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn combined_datahub_web_ui_serves_fedengine_and_mqtt_on_single_port() -> Result<()> {
    unsafe {
        std::env::set_var("FEDENGINE_PASSWORD", "test-fedengine-password");
    }
    let fixture = TempConfig::new(true, None);
    let config = Config::load(
        &fixture.path("combined-development.toml"),
        Service::Combined,
    )?;
    let mut composition = Composition::new(config.clone());
    let mut mqtt_plan = datahub_mqtt::Plan::load(&config)?;
    let fed_plan = datahub_fedengine::Plan::load(&config)?;
    let listen = fed_plan.management_listen()?;
    let (slot, fed_contrib) = fed_plan.web_contribution().unwrap();
    let mqtt_contrib = mqtt_plan.enable_web_admin();
    mqtt_plan.register(&mut composition)?;
    fed_plan.register_with_router_slot(&mut composition, Some(slot))?;
    WebGatewayPlan::new(
        listen,
        None,
        config.application.name.clone(),
        vec![fed_contrib, mqtt_contrib],
    )?
    .register(&mut composition)?;

    let running = composition.build()?.start().await?;
    let web_addr = running
        .endpoints
        .iter()
        .find(|e| e.protocol == "management")
        .unwrap()
        .address;

    let (status, caps) = http_call(web_addr, "GET", "/api/v1/capabilities", None, None).await;
    assert_eq!(status, 200);
    let caps: serde_json::Value = serde_json::from_str(&caps)?;
    assert_eq!(caps["services"], serde_json::json!(["fedengine", "mqtt"]));
    assert_eq!(caps["auth_required"], false);

    // Same port serves FedEngine API & JS AND MQTT API & JS!
    let (status, _) = http_call(web_addr, "GET", "/api/v1/sources", None, None).await;
    assert_eq!(status, 200);
    let (status, _) = http_call(web_addr, "GET", "/manage/app.js", None, None).await;
    assert_eq!(status, 200);
    let (status, stats) = http_call(web_addr, "GET", "/api/v1/mqtt/stats", None, None).await;
    assert_eq!(status, 200);
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&stats)?["active"],
        true
    );
    let (status, _) = http_call(web_addr, "GET", "/manage/mqtt.js", None, None).await;
    assert_eq!(status, 200);

    running.shutdown().await?;
    Ok(())
}
