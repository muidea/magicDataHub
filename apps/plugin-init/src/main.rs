//! Idempotent Init Container installer for optional magicDataHub database plugins.
#![forbid(unsafe_code)]
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::Read,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Component, Path, PathBuf},
    process,
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PluginManifest {
    name: String,
    version: String,
    duckdb_version: String,
    extension: ExtensionSpec,
    #[serde(default)]
    driver_subdir: Option<String>,
    #[serde(default)]
    odbc_ini: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExtensionSpec {
    key: String,
    path: String,
    sha256: String,
}

#[derive(Debug, Default)]
struct Stats {
    copied: usize,
    skipped: usize,
}

fn fail(message: impl std::fmt::Display) -> String {
    message.to_string()
}

fn valid_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
}

fn valid_relative_subpath(value: &str) -> Result<PathBuf, String> {
    let path = Path::new(value);
    if value.is_empty() || path.is_absolute() {
        return Err(format!("invalid relative path: {value}"));
    }
    let mut clean = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => clean.push(part),
            _ => return Err(format!("unsafe relative path component in: {value}")),
        }
    }
    if clean.as_os_str().is_empty() {
        return Err(format!("empty relative path: {value}"));
    }
    Ok(clean)
}

fn sha256_file(path: &Path) -> Result<(u64, String), String> {
    let mut file =
        File::open(path).map_err(|e| fail(format!("cannot open {}: {e}", path.display())))?;
    let len = file
        .metadata()
        .map_err(|e| fail(format!("cannot stat {}: {e}", path.display())))?
        .len();
    let mut hasher = Sha256::new();
    let mut buf = [0_u8; 64 * 1024];
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| fail(format!("cannot read {}: {e}", path.display())))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    let hex = hasher
        .finalize()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    Ok((len, hex))
}

fn temp_path_in(dir: &Path, label: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    dir.join(format!(".{label}.tmp-{}-{nanos}", process::id()))
}

fn write_atomic_bytes(
    dest: &Path,
    bytes: &[u8],
    mode: u32,
    stats: &mut Stats,
) -> Result<(), String> {
    if let Ok(meta) = fs::symlink_metadata(dest)
        && !meta.file_type().is_symlink()
        && meta.is_file()
        && meta.len() == bytes.len() as u64
        && let Ok(existing) = fs::read(dest)
        && existing == bytes
    {
        stats.skipped += 1;
        return Ok(());
    }
    let parent = dest
        .parent()
        .ok_or_else(|| format!("destination has no parent: {}", dest.display()))?;
    fs::create_dir_all(parent).map_err(|e| fail(format!("mkdir {}: {e}", parent.display())))?;
    if let Ok(meta) = fs::symlink_metadata(dest)
        && !meta.file_type().is_symlink()
        && meta.is_dir()
    {
        fs::remove_dir_all(dest)
            .map_err(|e| fail(format!("remove dir {}: {e}", dest.display())))?;
    }
    let tmp = temp_path_in(parent, "mdh-write");
    let result = (|| -> Result<(), String> {
        fs::write(&tmp, bytes).map_err(|e| fail(format!("write temp {}: {e}", tmp.display())))?;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(mode))
            .map_err(|e| fail(format!("chmod {}: {e}", tmp.display())))?;
        fs::rename(&tmp, dest).map_err(|e| fail(format!("rename {}: {e}", dest.display())))?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result?;
    stats.copied += 1;
    Ok(())
}

fn copy_file_if_changed(
    src: &Path,
    dest: &Path,
    mode: u32,
    expected_sha256: Option<&str>,
    stats: &mut Stats,
) -> Result<(), String> {
    let (src_len, src_sha) = sha256_file(src)?;
    if let Some(expected) = expected_sha256
        && src_sha != expected
    {
        return Err(format!(
            "checksum mismatch for {}: expected {expected}, got {src_sha}",
            src.display()
        ));
    }
    if let Ok(meta) = fs::symlink_metadata(dest)
        && !meta.file_type().is_symlink()
        && meta.is_file()
        && meta.len() == src_len
        && let Ok((_, dst_sha)) = sha256_file(dest)
        && dst_sha == src_sha
    {
        if (meta.permissions().mode() & 0o777) != (mode & 0o777) {
            let _ = fs::set_permissions(dest, fs::Permissions::from_mode(mode));
        }
        stats.skipped += 1;
        return Ok(());
    }
    let parent = dest
        .parent()
        .ok_or_else(|| format!("destination has no parent: {}", dest.display()))?;
    fs::create_dir_all(parent).map_err(|e| fail(format!("mkdir {}: {e}", parent.display())))?;
    let tmp = temp_path_in(parent, "mdh-copy");
    let result = (|| -> Result<(), String> {
        fs::copy(src, &tmp).map_err(|e| fail(format!("copy {}: {e}", src.display())))?;
        fs::set_permissions(&tmp, fs::Permissions::from_mode(mode))
            .map_err(|e| fail(format!("chmod {}: {e}", tmp.display())))?;
        fs::rename(&tmp, dest).map_err(|e| fail(format!("rename {}: {e}", dest.display())))?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result?;
    stats.copied += 1;
    Ok(())
}

fn sync_symlink(link_target: &Path, dest: &Path, stats: &mut Stats) -> Result<(), String> {
    if link_target.is_absolute() {
        return Err(format!(
            "refusing absolute symlink target in driver payload: {}",
            link_target.display()
        ));
    }
    if let Ok(meta) = fs::symlink_metadata(dest)
        && meta.file_type().is_symlink()
        && fs::read_link(dest).ok().as_deref() == Some(link_target)
    {
        stats.skipped += 1;
        return Ok(());
    }
    let parent = dest
        .parent()
        .ok_or_else(|| format!("destination has no parent: {}", dest.display()))?;
    fs::create_dir_all(parent).map_err(|e| fail(format!("mkdir {}: {e}", parent.display())))?;
    let tmp = temp_path_in(parent, "mdh-link");
    symlink(link_target, &tmp).map_err(|e| fail(format!("symlink {}: {e}", dest.display())))?;
    if let Err(err) = fs::rename(&tmp, dest) {
        let _ = fs::remove_file(&tmp);
        return Err(fail(format!("rename symlink {}: {err}", dest.display())));
    }
    stats.copied += 1;
    Ok(())
}

fn sync_driver_tree(src_root: &Path, dst_root: &Path, stats: &mut Stats) -> Result<(), String> {
    let mut stack = vec![(src_root.to_path_buf(), dst_root.to_path_buf())];
    while let Some((src_dir, dst_dir)) = stack.pop() {
        fs::create_dir_all(&dst_dir)
            .map_err(|e| fail(format!("mkdir {}: {e}", dst_dir.display())))?;
        let _ = fs::set_permissions(&dst_dir, fs::Permissions::from_mode(0o755));
        let mut entries: Vec<_> = fs::read_dir(&src_dir)
            .map_err(|e| fail(format!("read_dir {}: {e}", src_dir.display())))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| fail(format!("read_dir entry: {e}")))?;
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let src_path = entry.path();
            let dst_path = dst_dir.join(entry.file_name());
            let meta = fs::symlink_metadata(&src_path)
                .map_err(|e| fail(format!("stat {}: {e}", src_path.display())))?;
            if meta.file_type().is_symlink() {
                let target = fs::read_link(&src_path)
                    .map_err(|e| fail(format!("read_link {}: {e}", src_path.display())))?;
                sync_symlink(&target, &dst_path, stats)?;
            } else if meta.is_dir() {
                stack.push((src_path, dst_path));
            } else if meta.is_file() {
                let executable = (meta.permissions().mode() & 0o111) != 0;
                // Keep owner write bit so future upgrades can replace vendor libraries safely.
                let mode = if executable { 0o755 } else { 0o644 };
                copy_file_if_changed(&src_path, &dst_path, mode, None, stats)?;
            }
        }
    }
    Ok(())
}

fn merge_odbcinst(target_odbc: &Path, stats: &mut Stats) -> Result<(), String> {
    let ini_dir = target_odbc.join("odbcinst.d");
    fs::create_dir_all(&ini_dir).map_err(|e| fail(format!("mkdir {}: {e}", ini_dir.display())))?;
    let mut fragments = Vec::new();
    for entry in
        fs::read_dir(&ini_dir).map_err(|e| fail(format!("read_dir {}: {e}", ini_dir.display())))?
    {
        let entry = entry.map_err(|e| fail(format!("read_dir entry: {e}")))?;
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("ini") {
            continue;
        }
        let meta = fs::symlink_metadata(&path)
            .map_err(|e| fail(format!("stat {}: {e}", path.display())))?;
        if meta.file_type().is_symlink() || !meta.is_file() {
            return Err(format!("invalid odbc fragment: {}", path.display()));
        }
        fragments.push(path);
    }
    fragments.sort();
    let mut sections = Vec::new();
    for path in fragments {
        let content =
            fs::read_to_string(&path).map_err(|e| fail(format!("read {}: {e}", path.display())))?;
        let trimmed = content.trim();
        if !trimmed.is_empty() {
            sections.push(trimmed.to_owned());
        }
    }
    let merged = if sections.is_empty() {
        String::new()
    } else {
        format!("{}\n", sections.join("\n\n"))
    };
    write_atomic_bytes(
        &target_odbc.join("odbcinst.ini"),
        merged.as_bytes(),
        0o644,
        stats,
    )
}

fn install_plugin(source: &Path, target: &Path) -> Result<(String, String, Stats), String> {
    let manifest_bytes = fs::read(source.join("plugin.json"))
        .map_err(|e| fail(format!("cannot read {}/plugin.json: {e}", source.display())))?;
    let manifest: PluginManifest = serde_json::from_slice(&manifest_bytes)
        .map_err(|e| fail(format!("invalid plugin.json: {e}")))?;
    if !valid_segment(&manifest.name) || !valid_segment(&manifest.extension.key) {
        return Err("invalid plugin or extension identifier".into());
    }
    if manifest.version.trim().is_empty() {
        return Err("plugin version must not be empty".into());
    }
    if manifest.duckdb_version != "v2.0.0" {
        return Err(format!(
            "unsupported duckdb_version '{}'; expected 'v2.0.0'",
            manifest.duckdb_version
        ));
    }
    let ext_rel = valid_relative_subpath(&manifest.extension.path)?;
    if manifest.extension.sha256.len() != 64
        || !manifest
            .extension
            .sha256
            .bytes()
            .all(|b| b.is_ascii_hexdigit())
    {
        return Err("invalid extension sha256 digest".into());
    }

    fs::create_dir_all(target).map_err(|e| fail(format!("mkdir {}: {e}", target.display())))?;
    let lock_file = File::options()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(target.join(".plugin-init.lock"))
        .map_err(|e| fail(format!("cannot open plugin lock: {e}")))?;
    lock_file
        .lock()
        .map_err(|e| fail(format!("cannot lock plugin target: {e}")))?;

    let mut stats = Stats::default();
    let ext_src = source.join("extensions").join(&ext_rel);
    let ext_dst = target.join("extensions").join(&ext_rel);
    let ext_meta = fs::symlink_metadata(&ext_src).map_err(|e| {
        fail(format!(
            "missing extension payload {}: {e}",
            ext_src.display()
        ))
    })?;
    if ext_meta.file_type().is_symlink() || !ext_meta.is_file() {
        return Err(format!(
            "extension payload must be a regular file: {}",
            ext_src.display()
        ));
    }
    copy_file_if_changed(
        &ext_src,
        &ext_dst,
        0o644,
        Some(&manifest.extension.sha256),
        &mut stats,
    )?;

    let fragment = serde_json::json!({
        "duckdb_version": manifest.duckdb_version,
        "platform": "linux_amd64",
        "extensions": {
            manifest.extension.key: {
                "path": manifest.extension.path,
                "sha256": manifest.extension.sha256,
            }
        }
    });
    let mut fragment_bytes = serde_json::to_vec_pretty(&fragment).map_err(fail)?;
    fragment_bytes.push(b'\n');
    let fragment_path = target
        .join("extensions/manifest.d")
        .join(format!("{}.json", manifest.name));
    write_atomic_bytes(&fragment_path, &fragment_bytes, 0o644, &mut stats)?;

    if let Some(driver_subdir) = &manifest.driver_subdir {
        let rel = valid_relative_subpath(driver_subdir)?;
        if rel.components().count() != 1 {
            return Err("driver_subdir must be a single directory name".into());
        }
        let drv_src = source.join("drivers").join(&rel);
        if drv_src.is_dir() {
            sync_driver_tree(&drv_src, &target.join("drivers").join(&rel), &mut stats)?;
        }
    }

    if let Some(ini_name) = &manifest.odbc_ini {
        let rel = valid_relative_subpath(ini_name)?;
        if rel.components().count() != 1 || rel.extension().and_then(|s| s.to_str()) != Some("ini")
        {
            return Err("odbc_ini must be a single .ini filename".into());
        }
        let ini_src = source.join("odbc").join(&rel);
        let ini_bytes = fs::read(&ini_src)
            .map_err(|e| fail(format!("cannot read {}: {e}", ini_src.display())))?;
        let text = std::str::from_utf8(&ini_bytes)
            .map_err(|_| format!("invalid UTF-8 in {}", ini_src.display()))?;
        if text.trim().is_empty() {
            return Err(format!("empty odbc fragment {}", ini_src.display()));
        }
        let odbc_dir = target.join("odbc");
        write_atomic_bytes(
            &odbc_dir.join("odbcinst.d").join(&rel),
            &ini_bytes,
            0o644,
            &mut stats,
        )?;
        merge_odbcinst(&odbc_dir, &mut stats)?;
    }

    drop(lock_file);
    Ok((manifest.name, manifest.version, stats))
}

fn parse_args<I: Iterator<Item = String>>(
    mut args: I,
) -> Result<(bool, bool, PathBuf, PathBuf), String> {
    let mut source = PathBuf::from("/payload");
    let mut target = PathBuf::from("/plugins");
    let mut seen = BTreeSet::new();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => return Ok((true, false, source, target)),
            "-V" | "--version" => return Ok((false, true, source, target)),
            "--source" => {
                if !seen.insert("source") {
                    return Err("duplicate --source flag".into());
                }
                let val = args.next().ok_or("--source requires a directory path")?;
                source = PathBuf::from(val);
            }
            "--target" => {
                if !seen.insert("target") {
                    return Err("duplicate --target flag".into());
                }
                let val = args.next().ok_or("--target requires a directory path")?;
                target = PathBuf::from(val);
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok((false, false, source, target))
}

fn main() {
    let (help, version, source, target) = match parse_args(std::env::args().skip(1)) {
        Ok(parsed) => parsed,
        Err(err) => {
            eprintln!("error: {err}");
            process::exit(2);
        }
    };
    if help {
        println!("mdh-plugin-init [--source DIR] [--target DIR] | --version");
        return;
    }
    if version {
        println!("mdh-plugin-init {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    match install_plugin(&source, &target) {
        Ok((plugin, ver, stats)) => {
            let line = serde_json::json!({
                "event": "PluginInstalled",
                "plugin": plugin,
                "version": ver,
                "copied": stats.copied,
                "skipped": stats.skipped,
            });
            println!("{line}");
        }
        Err(err) => {
            eprintln!("error: {err}");
            process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    fn unique_temp(prefix: &str) -> PathBuf {
        static SEQ: AtomicU64 = AtomicU64::new(0);
        let id = SEQ.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!("{prefix}-{}-{id}", process::id()))
    }

    fn hex_sha(bytes: &[u8]) -> String {
        Sha256::digest(bytes)
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }

    fn write_sample_plugin(
        root: &Path,
        name: &str,
        ext_key: &str,
        ext_bytes: &[u8],
        driver_subdir: Option<&str>,
        odbc_ini: Option<(&str, &str)>,
    ) {
        let ext_rel = format!("v2.0.0/linux_amd64/{ext_key}_scanner.duckdb_extension");
        let ext_path = root.join("extensions").join(&ext_rel);
        fs::create_dir_all(ext_path.parent().unwrap()).unwrap();
        fs::write(&ext_path, ext_bytes).unwrap();
        if let Some(sub) = driver_subdir {
            let drv = root.join("drivers").join(sub);
            fs::create_dir_all(&drv).unwrap();
            let real_lib = drv.join("libdriver.so.1");
            fs::write(&real_lib, b"driver-so-payload").unwrap();
            fs::set_permissions(&real_lib, fs::Permissions::from_mode(0o444)).unwrap();
            symlink("libdriver.so.1", drv.join("libdriver.so")).unwrap();
        }
        let ini_filename = odbc_ini.map(|(file, body)| {
            fs::create_dir_all(root.join("odbc")).unwrap();
            fs::write(root.join("odbc").join(file), body).unwrap();
            file
        });
        let manifest = serde_json::json!({
            "name": name,
            "version": "0.1.0",
            "duckdb_version": "v2.0.0",
            "extension": {
                "key": ext_key,
                "path": ext_rel,
                "sha256": hex_sha(ext_bytes),
            },
            "driver_subdir": driver_subdir,
            "odbc_ini": ini_filename,
        });
        fs::write(
            root.join("plugin.json"),
            serde_json::to_vec_pretty(&manifest).unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn installs_merges_and_is_idempotent_across_plugins() {
        let root = unique_temp("mdh-plugin-init-test");
        let src1 = root.join("src-oracle");
        let src2 = root.join("src-db2");
        let dst = root.join("plugins");
        fs::create_dir_all(&src1).unwrap();
        fs::create_dir_all(&src2).unwrap();

        write_sample_plugin(
            &src1,
            "oracle",
            "oracle",
            b"oracle-ext",
            Some("oracle"),
            Some((
                "oracle.ini",
                "[Oracle 23 ODBC driver]\nDriver=/plugins/drivers/oracle/libdriver.so\n",
            )),
        );
        write_sample_plugin(
            &src2,
            "db2",
            "odbc_catalog",
            b"odbc-cat-ext",
            Some("db2"),
            Some((
                "db2.ini",
                "[IBM DB2 ODBC DRIVER]\nDriver=/plugins/drivers/db2/libdriver.so\n",
            )),
        );

        let (_, _, first) = install_plugin(&src1, &dst).unwrap();
        assert!(first.copied >= 5 && first.skipped == 0);
        let (_, _, second) = install_plugin(&src2, &dst).unwrap();
        assert!(second.copied >= 5);

        // Idempotent rerun skips already synchronized files/symlinks/fragments.
        let (_, _, rerun1) = install_plugin(&src1, &dst).unwrap();
        assert_eq!(rerun1.copied, 0);
        assert!(rerun1.skipped >= 6);
        let (_, _, rerun2) = install_plugin(&src2, &dst).unwrap();
        assert_eq!(rerun2.copied, 0);
        assert!(rerun2.skipped >= 6);

        let merged_ini = fs::read_to_string(dst.join("odbc/odbcinst.ini")).unwrap();
        assert!(merged_ini.contains("[IBM DB2 ODBC DRIVER]"));
        assert!(merged_ini.contains("[Oracle 23 ODBC driver]"));
        assert_eq!(
            fs::read_link(dst.join("drivers/oracle/libdriver.so")).unwrap(),
            Path::new("libdriver.so.1")
        );
        // Existing read-only o444 payload was normalized to owner-writable 0644/0755 on sync.
        let mode = fs::metadata(dst.join("drivers/oracle/libdriver.so.1"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(mode & 0o200, 0o200);

        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn rejects_checksum_mismatch_and_handles_concurrent_installs() {
        let root = unique_temp("mdh-plugin-init-conc");
        let dst = root.join("plugins");
        let bad = root.join("src-bad");
        fs::create_dir_all(&bad).unwrap();
        write_sample_plugin(&bad, "hana", "odbc_catalog", b"actual-bytes", None, None);
        fs::write(
            bad.join("extensions/v2.0.0/linux_amd64/odbc_catalog_scanner.duckdb_extension"),
            b"tampered-bytes",
        )
        .unwrap();
        assert!(
            install_plugin(&bad, &dst)
                .unwrap_err()
                .contains("checksum mismatch")
        );

        let src_a = root.join("src-a");
        let src_b = root.join("src-b");
        fs::create_dir_all(&src_a).unwrap();
        fs::create_dir_all(&src_b).unwrap();
        write_sample_plugin(
            &src_a,
            "dameng",
            "odbc_catalog",
            b"shared-odbc-cat",
            Some("dameng"),
            Some(("dameng.ini", "[DM8 ODBC DRIVER]\nDriver=/x\n")),
        );
        write_sample_plugin(
            &src_b,
            "hana",
            "odbc_catalog",
            b"shared-odbc-cat",
            Some("hana"),
            Some(("hana.ini", "[HDBODBC]\nDriver=/y\n")),
        );
        let dst_a = dst.clone();
        let dst_b = dst.clone();
        let t1 = std::thread::spawn(move || install_plugin(&src_a, &dst_a).unwrap());
        let t2 = std::thread::spawn(move || install_plugin(&src_b, &dst_b).unwrap());
        t1.join().unwrap();
        t2.join().unwrap();

        let merged = fs::read_to_string(dst.join("odbc/odbcinst.ini")).unwrap();
        assert!(merged.contains("[DM8 ODBC DRIVER]"));
        assert!(merged.contains("[HDBODBC]"));
        assert!(dst.join("extensions/manifest.d/dameng.json").is_file());
        assert!(dst.join("extensions/manifest.d/hana.json").is_file());
        fs::remove_dir_all(&root).unwrap();
    }
}
