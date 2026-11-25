"""BepInEx provisioning/restore, C#/Lua/INI mod code generation, dotnet compilation, and packaging."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from app.core.audit import append_audit_log
from app.core.config import (
    BUILT_MODS_DIR,
    DEFAULT_GAME_DIR,
    backup_dir_for_game,
    get_bepinex_bridge_roots,
    load_managed_games,
    manifest_path_for_game,
    sha256_file,
    sha256_text,
)
from app.core.constants import PLUGIN_DEPLOY_FILES
from app.engines.unity import fingerprint_unity_directory
from app.services.conflict_guard import inspect_conflict_guard
from app.services.discovery import make_slug
from app.services.game_snapshots import create_game_snapshot
from app.services.game_store import inspect_game_modding_info


def _cs_escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "")
        .replace("\n", "\\n")
    )


def _generate_il2cpp_plugin_cs(
    mod_id: str,
    mod_name: str,
    mod_version: str,
    assembly_name: str,
    apply_interval_sec: float,
    edits: list[Any],
) -> str:
    rules_lines: list[str] = []
    hook_lines: list[str] = []
    for idx, r in enumerate(edits):
        if getattr(r, "rule_type", "member_set") == "harmony_patch":
            target_type = _cs_escape(r.component_name or "TargetNamespace.TargetClass")
            target_method = _cs_escape(r.target_method or r.member_path or "TargetMethod")
            hook_kind = "Postfix" if str(getattr(r, "hook_kind", "Prefix")).lower() == "postfix" else "Prefix"
            skip_orig = "true" if getattr(r, "skip_original", False) else "false"
            hook_lines.append(
                f'            ApplyDynamicHarmonyHook(harmony, "{_cs_escape(r.label)}", "{target_type}", "{target_method}", "{hook_kind}", {skip_orig});'
            )
        else:
            cfg_key = f"Rule_{idx + 1}_{make_slug(r.label or r.member_path)[:24]}"
            rules_lines.append(
                f'            _rules.Add(new EditRule("{_cs_escape(r.label)}", "{_cs_escape(r.entity_name)}", '
                f'"{_cs_escape(r.component_name)}", "{_cs_escape(r.member_path)}", '
                f'Config.Bind("Edits", "{_cs_escape(cfg_key)}", "{_cs_escape(r.value)}", '
                f'"{_cs_escape(r.entity_name)} -> {_cs_escape(r.component_name)}.{_cs_escape(r.member_path)}")));'
            )
    rules_block = "\n".join(rules_lines)
    hooks_block = "\n".join(hook_lines)

    return f'''using System;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using BepInEx;
using BepInEx.Configuration;
using BepInEx.Logging;
using BepInEx.Unity.IL2CPP;
using HarmonyLib;
using Il2CppInterop.Runtime.Injection;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace {assembly_name}
{{
    [BepInPlugin("{_cs_escape(mod_id)}", "{_cs_escape(mod_name)}", "{_cs_escape(mod_version)}")]
    public class Plugin : BasePlugin
    {{
        internal static ManualLogSource ModLog = null!;
        private static ConfigEntry<bool> _enabled = null!;
        private static ConfigEntry<float> _intervalSec = null!;
        private static readonly List<EditRule> _rules = new();

        internal sealed class EditRule
        {{
            public string Label {{ get; }}
            public string EntityName {{ get; }}
            public string ComponentName {{ get; }}
            public string MemberPath {{ get; }}
            public ConfigEntry<string> ValueEntry {{ get; }}

            public EditRule(string label, string entityName, string componentName, string memberPath, ConfigEntry<string> valueEntry)
            {{
                Label = label;
                EntityName = entityName;
                ComponentName = componentName;
                MemberPath = memberPath;
                ValueEntry = valueEntry;
            }}
        }}

        public override void Load()
        {{
            ModLog = Log;
            _enabled = Config.Bind("General", "Enabled", true, "Enable or disable all mod edits.");
            _intervalSec = Config.Bind("General", "ApplyIntervalSeconds", {max(0.5, float(apply_interval_sec)):.1f}f, "How often (in seconds) the mod checks and applies active edits.");

{rules_block}
            var harmony = new Harmony("{_cs_escape(mod_id)}");
{hooks_block}

            ClassInjector.RegisterTypeInIl2Cpp<ModApplierBehaviour>();
            var host = new GameObject("{assembly_name}_Host");
            UnityEngine.Object.DontDestroyOnLoad(host);
            host.hideFlags = HideFlags.HideAndDontSave;
            host.AddComponent<ModApplierBehaviour>();

            SceneManager.sceneLoaded += (UnityEngine.Events.UnityAction<Scene, LoadSceneMode>)((scene, mode) =>
            {{
                ApplyAllRules();
            }});

            ModLog.LogInfo($"[{_cs_escape(mod_name)} v{_cs_escape(mod_version)}] Loaded with {{_rules.Count}} edit rule(s).");
        }}

        private static bool DynamicSkipPrefix() => false;
        private static void DynamicLogPrefix() {{ ModLog?.LogInfo("Harmony Prefix triggered."); }}
        private static void DynamicLogPostfix() {{ ModLog?.LogInfo("Harmony Postfix triggered."); }}

        private static void ApplyDynamicHarmonyHook(Harmony harmony, string label, string typeName, string methodName, string hookKind, bool skipOriginal)
        {{
            try
            {{
                var t = AccessTools.TypeByName(typeName);
                var m = t != null ? AccessTools.Method(t, methodName) : null;
                if (m == null)
                {{
                    ModLog.LogWarning($"Harmony hook '{{label}}' target not found: {{typeName}}.{{methodName}}");
                    return;
                }}
                if (hookKind == "Postfix")
                {{
                    harmony.Patch(m, postfix: new HarmonyMethod(typeof(Plugin), nameof(DynamicLogPostfix)));
                }}
                else
                {{
                    var fn = skipOriginal ? nameof(DynamicSkipPrefix) : nameof(DynamicLogPrefix);
                    harmony.Patch(m, prefix: new HarmonyMethod(typeof(Plugin), fn));
                }}
                ModLog.LogInfo($"Harmony {{hookKind}} patched {{typeName}}.{{methodName}} ({{label}})");
            }}
            catch (Exception ex)
            {{
                ModLog.LogWarning($"Harmony hook '{{label}}' failed: {{ex.Message}}");
            }}
        }}

        internal static void ApplyAllRules()
        {{
            if (!_enabled.Value) return;
            foreach (var rule in _rules)
            {{
                try
                {{
                    ApplyRule(rule);
                }}
                catch (Exception ex)
                {{
                    ModLog.LogWarning($"Rule '{{rule.Label}}' skipped: {{ex.Message}}");
                }}
            }}
        }}

        internal static float IntervalSeconds => Math.Max(0.5f, _intervalSec.Value);

        private static void ApplyRule(EditRule rule)
        {{
            var go = GameObject.Find(rule.EntityName);
            if (go == null) return;

            Component targetComp = null;
            foreach (var c in go.GetComponents<Component>())
            {{
                if (c == null) continue;
                var il2Type = c.GetIl2CppType();
                var fullName = il2Type?.FullName ?? c.GetType().FullName ?? "";
                if (fullName.Equals(rule.ComponentName, StringComparison.OrdinalIgnoreCase) ||
                    fullName.EndsWith("." + rule.ComponentName, StringComparison.OrdinalIgnoreCase))
                {{
                    targetComp = c;
                    break;
                }}
            }}
            if (targetComp == null) return;

            var parts = rule.MemberPath.Split('.', StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length == 0) return;

            object current = targetComp;
            for (int i = 0; i < parts.Length - 1; i++)
            {{
                var nextObj = GetMemberOrStat(current, parts[i]);
                if (nextObj == null) return;
                current = nextObj;
            }}

            SetMemberValue(current, parts[^1], rule.ValueEntry.Value);
        }}

        private static object GetMemberOrStat(object target, string name)
        {{
            var t = target.GetType();
            const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.IgnoreCase;
            var prop = t.GetProperty(name, flags);
            if (prop != null && prop.CanRead) return prop.GetValue(target);
            var fld = t.GetField(name, flags);
            if (fld != null) return fld.GetValue(target);

            var getStat = t.GetMethod("GetStatObject", flags);
            if (getStat != null)
            {{
                var pType = getStat.GetParameters()[0].ParameterType;
                if (pType.IsEnum && Enum.TryParse(pType, name, true, out var enumVal))
                {{
                    return getStat.Invoke(target, new[] {{ enumVal }});
                }}
            }}
            return null;
        }}

        private static void SetMemberValue(object target, string memberName, string rawValue)
        {{
            var t = target.GetType();
            const BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.IgnoreCase;
            var prop = t.GetProperty(memberName, flags);
            if (prop != null && prop.CanWrite)
            {{
                var converted = ConvertValue(rawValue, prop.PropertyType);
                prop.SetValue(target, converted);
                return;
            }}
            var fld = t.GetField(memberName, flags);
            if (fld != null)
            {{
                var converted = ConvertValue(rawValue, fld.FieldType);
                fld.SetValue(target, converted);
            }}
        }}

        private static object ConvertValue(string raw, Type targetType)
        {{
            var u = Nullable.GetUnderlyingType(targetType) ?? targetType;
            if (u == typeof(string)) return raw;
            if (u == typeof(bool)) return bool.Parse(raw.Trim());
            if (u == typeof(int)) return int.Parse(raw.Trim(), CultureInfo.InvariantCulture);
            if (u == typeof(float)) return float.Parse(raw.Trim(), CultureInfo.InvariantCulture);
            if (u == typeof(double)) return double.Parse(raw.Trim(), CultureInfo.InvariantCulture);
            if (u == typeof(Vector3))
            {{
                var clean = raw.Trim().Trim('(', ')');
                var xyz = clean.Split(',');
                return new Vector3(
                    float.Parse(xyz[0].Trim(), CultureInfo.InvariantCulture),
                    float.Parse(xyz[1].Trim(), CultureInfo.InvariantCulture),
                    float.Parse(xyz[2].Trim(), CultureInfo.InvariantCulture)
                );
            }}
            if (u.IsEnum) return Enum.Parse(u, raw.Trim(), true);
            return Convert.ChangeType(raw, u, CultureInfo.InvariantCulture);
        }}
    }}

    public class ModApplierBehaviour : MonoBehaviour
    {{
        private float _timer = 0f;

        public ModApplierBehaviour(IntPtr ptr) : base(ptr) {{ }}

        private void Update()
        {{
            _timer += Time.deltaTime;
            if (_timer >= Plugin.IntervalSeconds)
            {{
                _timer = 0f;
                Plugin.ApplyAllRules();
            }}
        }}
    }}
}}
'''


def _generate_il2cpp_csproj(assembly_name: str, bep_core_dir: Path, interop_dir: Path) -> str:
    core_dlls = [
        ("BepInEx.Core", bep_core_dir / "BepInEx.Core.dll"),
        ("BepInEx.Unity.IL2CPP", bep_core_dir / "BepInEx.Unity.IL2CPP.dll"),
        ("BepInEx.Unity.Common", bep_core_dir / "BepInEx.Unity.Common.dll"),
        ("0Harmony", bep_core_dir / "0Harmony.dll"),
        ("Il2CppInterop.Runtime", bep_core_dir / "Il2CppInterop.Runtime.dll"),
        ("Il2CppInterop.Common", bep_core_dir / "Il2CppInterop.Common.dll"),
    ]
    interop_dlls = [
        ("Il2Cppmscorlib", interop_dir / "Il2Cppmscorlib.dll"),
        ("Il2CppSystem", interop_dir / "Il2CppSystem.dll"),
        ("UnityEngine", interop_dir / "UnityEngine.dll"),
        ("UnityEngine.CoreModule", interop_dir / "UnityEngine.CoreModule.dll"),
        ("UnityEngine.SceneManagementModule", interop_dir / "UnityEngine.SceneManagementModule.dll"),
    ]
    refs: list[str] = []
    for name, path in core_dlls + interop_dlls:
        if path.is_file():
            refs.append(f'    <Reference Include="{name}"><HintPath>{path}</HintPath><Private>false</Private></Reference>')
    refs_block = "\n".join(refs)
    return f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net6.0</TargetFramework>
    <AssemblyName>{assembly_name}</AssemblyName>
    <LangVersion>latest</LangVersion>
    <Nullable>disable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="Plugin.cs" />
  </ItemGroup>
  <ItemGroup>
{refs_block}
  </ItemGroup>
</Project>
'''


def provision_game_impl(
    game_dir_str: str,
    bepinex_zip_str: str,
    expected_sha256: str,
    plugin_build_dir_str: str,
    *,
    force: bool = False,
    game_id: str = "",
) -> dict[str, Any]:
    """Verify archive SHA-256, back up colliding files, extract BepInEx, and deploy MCP bridge plugin."""
    game_dir = Path(game_dir_str.strip())
    zip_path = Path(bepinex_zip_str.strip())
    plugin_build_dir = Path(plugin_build_dir_str.strip())
    if not plugin_build_dir.is_dir() and "bepinex-mcp" in str(plugin_build_dir).lower():
        for root in get_bepinex_bridge_roots():
            candidate = root / "plugins" / "BepInExMCP.IL2CPP" / "bin" / "Release" / "net6.0"
            if candidate.is_dir():
                plugin_build_dir = candidate
                break
    expected_hash = expected_sha256.strip().lower()

    if not game_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Game folder not found: {game_dir}")

    guard = inspect_conflict_guard(game_dir, intended_action="provision")
    blocking = [
        w for w in guard.get("warnings", [])
        if w.get("code") in ("mod_manager_detected", "proxy_dll_winhttp")
    ]
    if blocking and not force:
        return {
            "status": "conflict_warning",
            "requires_confirmation": True,
            "summary": guard["summary"],
            "warnings": guard["warnings"],
            "conflict_guard": guard,
        }

    pre_snapshot_id = ""
    try:
        snap = create_game_snapshot(
            game_id or game_dir.name,
            game_dir,
            label="Pre-provision BepInEx 6 IL2CPP",
            engine="unity",
        )
        pre_snapshot_id = snap.get("snapshot_id", "")
    except ValueError:
        pre_snapshot_id = ""

    if not zip_path.is_file():
        raise HTTPException(status_code=400, detail=f"BepInEx zip not found: {zip_path}")
    if not plugin_build_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Plugin build dir not found: {plugin_build_dir}")

    fp = fingerprint_unity_directory(game_dir)
    if fp["blocked"]:
        raise HTTPException(
            status_code=403,
            detail=f"Provisioning blocked due to anti-cheat binaries: {fp['anti_cheat_detected']}",
        )

    actual_hash = sha256_file(zip_path)
    if expected_hash and actual_hash != expected_hash:
        append_audit_log(
            action="provision",
            status="error",
            summary=f"SHA-256 mismatch for {zip_path.name}",
            sha256=actual_hash,
            details={"expected": expected_hash, "actual": actual_hash},
        )
        raise HTTPException(
            status_code=400,
            detail=f"SHA-256 mismatch! Expected {expected_hash}, got {actual_hash}",
        )

    manifest_file = manifest_path_for_game(game_dir)
    backup_dir = backup_dir_for_game(game_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    existing_manifest: dict[str, Any] = {}
    if manifest_file.is_file():
        try:
            existing_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:
            existing_manifest = {}

    backed_up_files: dict[str, dict[str, str]] = dict(existing_manifest.get("backed_up_files", {}))
    added_files: dict[str, str] = dict(existing_manifest.get("added_files", {}))

    def backup_if_colliding(rel_path_str: str) -> None:
        target = game_dir / rel_path_str
        norm_rel = Path(rel_path_str).as_posix()
        if target.is_file() and norm_rel not in backed_up_files and norm_rel not in added_files:
            b_dest = backup_dir / norm_rel
            b_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, b_dest)
            backed_up_files[norm_rel] = {
                "backup_path": str(b_dest),
                "sha256": sha256_file(b_dest),
            }

    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            rel_member = Path(member.filename).as_posix()
            backup_if_colliding(rel_member)
            dest_path = game_dir / rel_member
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member, "r") as src, dest_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            if rel_member not in backed_up_files:
                added_files[rel_member] = sha256_file(dest_path)

    plugin_target_dir = game_dir / "BepInEx" / "plugins" / "BepInExMCP.IL2CPP"
    plugin_target_dir.mkdir(parents=True, exist_ok=True)
    deployed_plugin_files: list[str] = []

    for fname in PLUGIN_DEPLOY_FILES:
        src_file = plugin_build_dir / fname
        if not src_file.is_file():
            if fname.endswith(".pdb"):
                continue
            raise HTTPException(status_code=400, detail=f"Missing compiled plugin file: {src_file}")
        rel_dest = (Path("BepInEx") / "plugins" / "BepInExMCP.IL2CPP" / fname).as_posix()
        backup_if_colliding(rel_dest)
        dest_file = game_dir / rel_dest
        shutil.copy2(src_file, dest_file)
        file_hash = sha256_file(dest_file)
        if rel_dest not in backed_up_files:
            added_files[rel_dest] = file_hash
        deployed_plugin_files.append(rel_dest)

    plugin_dll_hash = sha256_file(plugin_target_dir / "BepInExMCP.IL2CPP.dll")
    manifest_data = {
        "game_dir": str(game_dir),
        "bepinex_zip_path": str(zip_path),
        "bepinex_zip_sha256": actual_hash,
        "plugin_dll_sha256": plugin_dll_hash,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "added_files": added_files,
        "backed_up_files": backed_up_files,
        "deployed_plugin_files": deployed_plugin_files,
    }
    manifest_file.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    summary = (
        f"Provisioned BepInEx 6 IL2CPP + BepInExMCP.IL2CPP.dll "
        f"({len(added_files)} tracked added files, {len(backed_up_files)} backed up)"
    )
    append_audit_log(
        action="provision",
        status="ok",
        summary=summary,
        sha256=actual_hash,
        details={
            "manifest_path": str(manifest_file),
            "zip_sha256": actual_hash,
            "plugin_dll_sha256": plugin_dll_hash,
            "added_count": len(added_files),
            "backed_up_count": len(backed_up_files),
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "manifest_path": str(manifest_file),
        "bepinex_zip_sha256": actual_hash,
        "plugin_dll_sha256": plugin_dll_hash,
        "added_files_count": len(added_files),
        "backed_up_files_count": len(backed_up_files),
        "deployed_plugin_files": deployed_plugin_files,
        "snapshot_id": pre_snapshot_id,
        "conflict_warnings": guard.get("warnings", []),
    }


def restore_game_impl(game_dir_str: str) -> dict[str, Any]:
    """Revert provisioned files and restore backed-up colliding files for *game_dir_str*."""
    game_dir = Path(game_dir_str.strip())
    if not game_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Game folder not found: {game_dir}")

    manifest_file = manifest_path_for_game(game_dir)
    backup_dir = backup_dir_for_game(game_dir)
    deleted_files: list[str] = []
    restored_files: list[str] = []

    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        for rel_path in manifest.get("added_files", {}):
            target = game_dir / rel_path
            if target.is_file() or target.is_symlink():
                target.unlink()
                deleted_files.append(rel_path)

        for rel_path, meta in manifest.get("backed_up_files", {}).items():
            b_src = Path(meta["backup_path"])
            target = game_dir / rel_path
            if b_src.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(b_src, target)
                restored_files.append(rel_path)

    backed_up_set = set(restored_files)
    for root_fname in (
        "winhttp.dll",
        "doorstop_config.ini",
        ".doorstop_version",
        "changelog.txt",
        "libdoorstop.dylib",
        "libdoorstop.so",
        "run_bepinex.sh",
    ):
        if root_fname not in backed_up_set:
            candidate = game_dir / root_fname
            if candidate.is_file():
                candidate.unlink()
                if root_fname not in deleted_files:
                    deleted_files.append(root_fname)

    for gen_dir_name in ("BepInEx", "dotnet", "mono"):
        gen_dir = game_dir / gen_dir_name
        if gen_dir.is_dir() and not any(r.startswith(f"{gen_dir_name}/") for r in backed_up_set):
            shutil.rmtree(gen_dir)
            deleted_files.append(f"{gen_dir_name}/")

    if manifest_file.is_file():
        manifest_file.unlink()
    if backup_dir.is_dir():
        shutil.rmtree(backup_dir)

    residuals: list[str] = []
    for check_item in ("BepInEx", "dotnet", "winhttp.dll", "doorstop_config.ini", ".doorstop_version"):
        if (game_dir / check_item).exists() and check_item not in backed_up_set:
            residuals.append(check_item)

    zero_residual = len(residuals) == 0
    summary = (
        f"Clean restore completed: deleted {len(deleted_files)} item(s), "
        f"restored {len(restored_files)} backup(s), zero_residual={zero_residual}"
    )
    append_audit_log(
        action="restore",
        status="ok" if zero_residual else "warning",
        summary=summary,
        details={
            "deleted_count": len(deleted_files),
            "restored_count": len(restored_files),
            "zero_residual": zero_residual,
            "residuals": residuals,
        },
    )
    return {
        "status": "ok" if zero_residual else "residual_detected",
        "summary": summary,
        "deleted_items": deleted_files,
        "restored_files": restored_files,
        "zero_residual": zero_residual,
        "residuals": residuals,
    }


async def build_compatible_mod_impl(
    req: Any,
    default_rule_factory: Callable[[], Any],
    *,
    built_mods_dir: Path | None = None,
    inspect_fn: Callable[[Path, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a game-compatible mod package (.dll/.lua/.ini + manifest + instructions + .zip)."""
    store = load_managed_games()
    target_game = next((g for g in store.get("games", []) if g.get("id") == req.game_id), None)
    game_dir = Path((req.game_dir or (target_game.get("game_dir") if target_game else DEFAULT_GAME_DIR)).strip())
    game_name = target_game.get("name", game_dir.name) if target_game else game_dir.name
    inspector = inspect_fn or inspect_game_modding_info
    info = (
        target_game.get("cached_modding_info")
        if (target_game and isinstance(target_game.get("cached_modding_info"), dict))
        else inspector(game_dir, game_name)
    )

    edits = req.edits or [default_rule_factory()]
    mod_slug = make_slug(req.mod_name)
    assembly_name = "".join(w.capitalize() for w in mod_slug.split("_") if w) or "CustomGameMod"
    base_built_dir = built_mods_dir or BUILT_MODS_DIR
    out_dir = base_built_dir / mod_slug
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    has_ga = game_dir.is_dir() and (game_dir / "GameAssembly.dll").is_file()
    managed_dirs = [d / "Managed" for d in game_dir.glob("*_Data") if (d / "Managed" / "Assembly-CSharp.dll").is_file()] if game_dir.is_dir() else []
    has_mono = (not has_ga) and bool(managed_dirs)
    is_unreal = bool(info.get("is_unreal_engine") or info.get("mcp_category") == "Unreal Engine 4/5")
    bep_core_dir = game_dir / "BepInEx" / "core"
    interop_dir = game_dir / "BepInEx" / "interop"

    built_files: list[str] = []
    compile_stdout = ""
    mod_kind = ""
    compatibility_notes = ""
    install_steps: list[str] = []
    zip_path = out_dir / f"{mod_slug}.zip"

    if has_ga:
        mod_kind = f"BepInEx 6 Unity.IL2CPP Plugin ({info.get('unity_version', 'Unity')} | Metadata v{info.get('metadata_version', '?')})"
        if not bep_core_dir.is_dir() or not interop_dir.is_dir():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"To compile an ABI-compatible IL2CPP mod for '{game_name}', BepInEx\\core and BepInEx\\interop "
                    f"must exist in '{game_dir}'. Run Provision & Backup and launch the game once first."
                ),
            )

        src_dir = out_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        plugin_cs = _generate_il2cpp_plugin_cs(
            req.mod_id, req.mod_name, req.mod_version, assembly_name, req.apply_interval_sec, edits
        )
        csproj_xml = _generate_il2cpp_csproj(assembly_name, bep_core_dir, interop_dir)
        (src_dir / "Plugin.cs").write_text(plugin_cs, encoding="utf-8")
        (src_dir / f"{assembly_name}.csproj").write_text(csproj_xml, encoding="utf-8")

        proc = await asyncio.to_thread(
            subprocess.run,
            ["dotnet", "build", str(src_dir / f"{assembly_name}.csproj"), "-c", "Release", "--nologo"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        compile_stdout = proc.stdout or ""
        compile_stderr = proc.stderr or ""
        if proc.returncode != 0:
            append_audit_log(
                action="mods.build",
                status="error",
                summary=f"Failed to compile IL2CPP mod '{req.mod_name}' for {game_name}",
                details={"stdout": compile_stdout, "stderr": compile_stderr},
            )
            raise HTTPException(
                status_code=500,
                detail=f"dotnet build failed:\n{compile_stdout}\n{compile_stderr}",
            )

        built_dll = src_dir / "bin" / "Release" / "net6.0" / f"{assembly_name}.dll"
        if not built_dll.is_file():
            raise HTTPException(status_code=500, detail=f"Compiled DLL not found at {built_dll}")

        pkg_root = out_dir / "package"
        plugins_out = pkg_root / "BepInEx" / "plugins" / assembly_name
        plugins_out.mkdir(parents=True, exist_ok=True)
        dest_dll = plugins_out / f"{assembly_name}.dll"
        shutil.copy2(built_dll, dest_dll)
        built_files.append(str(dest_dll))

        compatibility_notes = (
            f"Compiled directly against {game_name}'s Unity {info.get('unity_version')} Il2CppInterop assemblies "
            f"(assembly-hash: {info.get('assembly_hash', 'N/A')}) and BepInEx {info.get('bepinex_version', '6.x')}."
        )
        install_steps = [
            f"1. Ensure BepInEx 6 (Unity.IL2CPP x64) is installed in '{game_dir}' (winhttp.dll + doorstop_config.ini + BepInEx\\core).",
            f"2. Extract '{zip_path.name}' directly into '{game_dir}' (or click 'Install Built Mod to Game' in this tab).",
            f"3. Confirm the compiled DLL is at: {game_dir / 'BepInEx' / 'plugins' / assembly_name / (assembly_name + '.dll')}",
            f"4. Launch '{info.get('executable', game_name)}'. The mod automatically applies all {len(edits)} edit rule(s) on scene load and every {req.apply_interval_sec:.1f}s.",
            f"5. To customize values later without recompiling, edit '{game_dir / 'BepInEx' / 'config' / (req.mod_id + '.cfg')}' after first launch.",
        ]
    elif has_mono:
        managed_dir = managed_dirs[0]
        mod_kind = f"BepInEx 5 Unity Mono Plugin ({info.get('unity_version', 'Unity')})"
        bep_mono_dll = bep_core_dir / "BepInEx.dll"
        if not bep_mono_dll.is_file():
            raise HTTPException(
                status_code=400,
                detail=f"To compile a Unity Mono plugin for '{game_name}', BepInEx 5 (BepInEx\\core\\BepInEx.dll) must be installed in '{game_dir}'.",
            )
        src_dir = out_dir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        mono_rules = "\n".join(
            f'            _rules.Add(new System.Tuple<string, string, string, string>("{_cs_escape(r.entity_name)}", "{_cs_escape(r.component_name)}", "{_cs_escape(r.member_path)}", "{_cs_escape(r.value)}"));'
            for r in edits
        )
        mono_cs = f'''using System;
using System.Collections.Generic;
using BepInEx;
using UnityEngine;

namespace {assembly_name}
{{
    [BepInPlugin("{_cs_escape(req.mod_id)}", "{_cs_escape(req.mod_name)}", "{_cs_escape(req.mod_version)}")]
    public class Plugin : BaseUnityPlugin
    {{
        private readonly List<Tuple<string, string, string, string>> _rules = new List<Tuple<string, string, string, string>>();
        private float _timer = 0f;

        private void Awake()
        {{
{mono_rules}
            Logger.LogInfo("[{_cs_escape(req.mod_name)} v{_cs_escape(req.mod_version)}] Loaded with " + _rules.Count + " rule(s).");
        }}

        private void Update()
        {{
            _timer += Time.deltaTime;
            if (_timer < {max(0.5, float(req.apply_interval_sec)):.1f}f) return;
            _timer = 0f;
            foreach (var r in _rules)
            {{
                var go = GameObject.Find(r.Item1);
                if (go == null) continue;
                var comp = go.GetComponent(r.Item2);
                if (comp == null) continue;
                var t = comp.GetType();
                var prop = t.GetProperty(r.Item3);
                if (prop != null && prop.CanWrite) {{ prop.SetValue(comp, Convert.ChangeType(r.Item4, prop.PropertyType), null); continue; }}
                var fld = t.GetField(r.Item3);
                if (fld != null) fld.SetValue(comp, Convert.ChangeType(r.Item4, fld.FieldType));
            }}
        }}
    }}
}}
'''
        mono_refs = "\n".join(
            f'    <Reference Include="{p.stem}"><HintPath>{p}</HintPath><Private>false</Private></Reference>'
            for p in [
                bep_mono_dll,
                bep_core_dir / "0Harmony.dll",
                managed_dir / "UnityEngine.dll",
                managed_dir / "UnityEngine.CoreModule.dll",
                managed_dir / "Assembly-CSharp.dll",
            ]
            if p.is_file()
        )
        mono_csproj = f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net472</TargetFramework>
    <AssemblyName>{assembly_name}</AssemblyName>
    <LangVersion>latest</LangVersion>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup><Compile Include="Plugin.cs" /></ItemGroup>
  <ItemGroup>
{mono_refs}
  </ItemGroup>
</Project>
'''
        (src_dir / "Plugin.cs").write_text(mono_cs, encoding="utf-8")
        (src_dir / f"{assembly_name}.csproj").write_text(mono_csproj, encoding="utf-8")
        proc = await asyncio.to_thread(
            subprocess.run,
            ["dotnet", "build", str(src_dir / f"{assembly_name}.csproj"), "-c", "Release", "--nologo"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        compile_stdout = proc.stdout or ""
        compile_stderr = proc.stderr or ""
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"dotnet build failed:\n{compile_stdout}\n{compile_stderr}")
        built_dll = src_dir / "bin" / "Release" / "net472" / f"{assembly_name}.dll"
        pkg_root = out_dir / "package"
        plugins_out = pkg_root / "BepInEx" / "plugins" / assembly_name
        plugins_out.mkdir(parents=True, exist_ok=True)
        dest_dll = plugins_out / f"{assembly_name}.dll"
        shutil.copy2(built_dll, dest_dll)
        built_files.append(str(dest_dll))
        compatibility_notes = f"Compiled directly against {game_name}'s Unity Mono Managed assemblies and BepInEx 5."
        install_steps = [
            f"1. Ensure BepInEx 5 is installed in '{game_dir}'.",
            f"2. Extract '{zip_path.name}' into '{game_dir}' (or click 'Install Built Mod to Game').",
            f"3. Launch '{info.get('executable', game_name)}'.",
        ]
    elif is_unreal:
        mod_kind = f"Unreal Engine 4/5 RE-UE4SS Lua Mod + INI Override ({info.get('detected_project_name', game_name)})"
        pkg_root = out_dir / "package"
        scripts_dir = pkg_root / "Mods" / assembly_name / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (pkg_root / "Mods" / assembly_name / "enabled.txt").write_text("", encoding="utf-8")

        lua_lines = [
            f'-- {req.mod_name} v{req.mod_version} by {req.author}',
            f'print("[{req.mod_name}] Loaded UE4SS Lua Mod\\n")',
        ]
        ini_lines = [f"; {req.mod_name} v{req.mod_version} Unreal INI Overrides", "[/Script/Engine.GameUserSettings]"]
        for r in edits:
            if r.member_path.startswith("sg.") or "Resolution" in r.member_path or "FrameRate" in r.member_path or "VSync" in r.member_path:
                ini_lines.append(f"{r.member_path}={r.value}")
            else:
                lua_lines.append(f'-- Rule: {r.label} ({r.entity_name}.{r.member_path} = {r.value})')
                lua_lines.append(
                    f'RegisterHook("/Script/Engine.PlayerController:ClientRestart", function(self)\n'
                    f'    print("[{req.mod_name}] Applying {r.label}\\n")\n'
                    f'end)'
                )
        (scripts_dir / "main.lua").write_text("\n".join(lua_lines), encoding="utf-8")
        (pkg_root / "GameUserSettings_Override.ini").write_text("\n".join(ini_lines), encoding="utf-8")
        built_files.extend([str(scripts_dir / "main.lua"), str(pkg_root / "GameUserSettings_Override.ini")])
        compatibility_notes = f"Compatible with RE-UE4SS (v3.0+) and Unreal Engine 4/5 INI configuration for {game_name}."
        install_steps = [
            f"1. Install RE-UE4SS into the game's Win64 binaries folder (next to the Shipping .exe).",
            f"2. Copy 'Mods\\{assembly_name}' from '{zip_path.name}' into your game's 'Win64\\Mods\\' folder.",
            f"3. Ensure 'Mods\\{assembly_name}\\enabled.txt' is present (already included in the package).",
            f"4. Merge any INI overrides from 'GameUserSettings_Override.ini' into your %LOCALAPPDATA% Unreal Saved\\Config\\Windows\\GameUserSettings.ini.",
        ]
    else:
        mod_kind = f"Data-Driven / Config Mod Package ({info.get('mcp_category', 'PC')})"
        pkg_root = out_dir / "package"
        pkg_root.mkdir(parents=True, exist_ok=True)
        patch_payload = {
            "mod_id": req.mod_id,
            "mod_name": req.mod_name,
            "version": req.mod_version,
            "author": req.author,
            "target_game": game_name,
            "edits": [r.model_dump() for r in edits],
        }
        patch_file = pkg_root / f"{mod_slug}_patch.json"
        patch_file.write_text(json.dumps(patch_payload, indent=2), encoding="utf-8")
        built_files.append(str(patch_file))
        compatibility_notes = f"Formatted for {game_name} ({info.get('mcp_category', 'PC')})."
        install_steps = [
            f"1. Extract '{zip_path.name}' into '{game_dir}'.",
            f"2. Apply '{patch_file.name}' using the game's native mod loader or data override directory.",
        ]

    manifest_data = {
        "mod_id": req.mod_id,
        "mod_name": req.mod_name,
        "mod_slug": mod_slug,
        "assembly_name": assembly_name,
        "version": req.mod_version,
        "author": req.author,
        "target_game_id": req.game_id,
        "target_game_name": game_name,
        "mod_kind": mod_kind,
        "compatibility": compatibility_notes,
        "edits": [r.model_dump() for r in edits],
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    pkg_root = out_dir / "package"
    (pkg_root / "mod_manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    instructions_text = (
        f"=== {req.mod_name} (v{req.mod_version}) ===\n"
        f"Target Game: {game_name}\n"
        f"Mod Type: {mod_kind}\n"
        f"Compatibility: {compatibility_notes}\n\n"
        f"INSTALLATION INSTRUCTIONS:\n"
        + "\n".join(install_steps)
        + "\n"
    )
    (pkg_root / "INSTALL_INSTRUCTIONS.txt").write_text(instructions_text, encoding="utf-8")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in pkg_root.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(pkg_root))

    zip_hash = sha256_file(zip_path)
    summary = f"Built compatible mod '{req.mod_name}' ({mod_kind}) -> {zip_path.name} ({len(edits)} edit rule(s))"
    append_audit_log(
        action="mods.build",
        status="ok",
        summary=summary,
        sha256=zip_hash,
        details={"mod_slug": mod_slug, "zip_path": str(zip_path), "game_id": req.game_id, "edits_count": len(edits)},
    )
    return {
        "status": "ok",
        "summary": summary,
        "mod_slug": mod_slug,
        "assembly_name": assembly_name,
        "mod_kind": mod_kind,
        "compatibility": compatibility_notes,
        "output_dir": str(out_dir),
        "package_dir": str(pkg_root),
        "zip_path": str(zip_path),
        "zip_sha256": zip_hash,
        "built_files": built_files,
        "install_steps": install_steps,
        "instructions_text": instructions_text,
        "compile_output": compile_stdout.strip(),
    }


def install_built_mod_impl(
    mod_slug: str,
    game_id: str,
    game_dir_str: str,
    *,
    force: bool = False,
    built_mods_dir: Path | None = None,
) -> dict[str, Any]:
    """Copy a built mod package's payload directly into the target game folder."""
    base_built_dir = built_mods_dir or BUILT_MODS_DIR
    pkg_root = base_built_dir / mod_slug.strip() / "package"
    if not pkg_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Built mod package '{mod_slug}' not found.")

    game_dir = Path(game_dir_str.strip())
    if not game_dir.is_dir():
        raise HTTPException(status_code=400, detail=f"Target game directory does not exist: {game_dir}")

    payload_files = [
        src for src in pkg_root.rglob("*")
        if src.is_file() and src.relative_to(pkg_root).name not in ("mod_manifest.json", "INSTALL_INSTRUCTIONS.txt")
    ]
    has_pak_override = any(src.suffix.lower() == ".pak" for src in payload_files)
    guard = inspect_conflict_guard(game_dir, intended_action="pak_override" if has_pak_override else "mod_install")
    if guard.get("warnings") and not force and (guard.get("has_mod_manager") or has_pak_override):
        return {
            "status": "conflict_warning",
            "requires_confirmation": True,
            "summary": guard["summary"],
            "warnings": guard["warnings"],
            "conflict_guard": guard,
        }

    pre_snapshot_id = ""
    try:
        snap = create_game_snapshot(
            game_id or game_dir.name,
            game_dir,
            label=f"Pre-deploy mod {mod_slug}",
            audit=True,
        )
        pre_snapshot_id = snap.get("snapshot_id", "")
    except ValueError:
        pre_snapshot_id = ""

    installed_files: list[str] = []
    for src in payload_files:
        rel = src.relative_to(pkg_root)
        dest = game_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        installed_files.append(str(dest))

    mod_hash = sha256_text(json.dumps(installed_files, sort_keys=True))
    summary = f"Installed built mod '{mod_slug}' ({len(installed_files)} file(s)) into {game_dir}"
    append_audit_log(
        action="mods.install",
        status="ok",
        summary=summary,
        sha256=mod_hash,
        details={
            "mod_slug": mod_slug,
            "game_id": game_id,
            "installed_files": installed_files,
            "snapshot_id": pre_snapshot_id,
        },
    )
    return {
        "status": "ok",
        "summary": summary,
        "installed_files": installed_files,
        "sha256": mod_hash,
        "snapshot_id": pre_snapshot_id,
        "conflict_warnings": guard.get("warnings", []),
    }
