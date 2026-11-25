import { apiFetch, apiPost, runTrackedAction, setOutput, state } from "./api.js";
import { loadLogs } from "./app_mgmt.js";
import { refreshGamesList, rescanActiveGameMetadata } from "./library.js";

function _resolveMcpBtn(fallbackId = "") {
  const evTarget = typeof window !== "undefined" ? window.event?.target : null;
  const fromEvent = evTarget && typeof evTarget.closest === "function" ? evTarget.closest("button") : null;
  return fromEvent || (fallbackId ? document.getElementById(fallbackId) : null);
}

export const ENTITIES = {
  paladin_piece: {
    goId: "-269924",
    scene: "27653627778046a48b230901ca6f384c",
    path: "/LevelSceneRoot[0]/Piece_DragonbornPaladin%28Clone%29[10]",
    name: "Piece_DragonbornPaladin(Clone)"
  },
  ranger_piece: {
    goId: "-270568",
    scene: "27653627778046a48b230901ca6f384c",
    path: "/LevelSceneRoot[0]/Piece_HumanRanger%28Clone%29[11]",
    name: "Piece_HumanRanger(Clone)"
  },
  rogue_piece: {
    goId: "-271206",
    scene: "27653627778046a48b230901ca6f384c",
    path: "/LevelSceneRoot[0]/Piece_TieflingRogue%28Clone%29[12]",
    name: "Piece_TieflingRogue(Clone)"
  },
  bard_piece: {
    goId: "-271750",
    scene: "27653627778046a48b230901ca6f384c",
    path: "/LevelSceneRoot[0]/Piece_ElfBard%28Clone%29[13]",
    name: "Piece_ElfBard(Clone)"
  },
  goblin_archer: {
    goId: "-272266",
    scene: "27653627778046a48b230901ca6f384c",
    path: "/LevelSceneRoot[0]/Piece_GoblinArcher%28Clone%29[14]",
    name: "Piece_GoblinArcher(Clone)"
  },
  babris_npc: {
    goId: "-275866",
    scene: "27653627778046a48b230901ca6f384c",
    path: "/LevelSceneRoot[0]/Piece_Babris%28Clone%29[15]",
    name: "Piece_Babris(Clone)"
  },
  avatar_nonvr: {
    goId: "-1582",
    scene: "Main",
    path: "/StandaloneAvatarNonVR%28Clone%29[0]",
    name: "StandaloneAvatarNonVR(Clone)"
  },
  network_avatar: {
    goId: "-147658",
    scene: "Main",
    path: "/NetworkedXRAvatar%28Clone%29[0]",
    name: "NetworkedXRAvatar(Clone)"
  },
  player_input: {
    goId: "-1544",
    scene: "DontDestroyOnLoad",
    path: "/NonVrInput%28Clone%29[0]/PlayerInputNonVR[0]",
    name: "PlayerInputNonVR"
  },
  hud_hp_text_1: {
    goId: "-64174",
    scene: "DontDestroyOnLoad",
    path: "/ScreenspaceHudViewNonVR%28Clone%29[0]/UICanvas[0]/LeftStretchAnchor[0]/LeftStretchOffset[0]/TopLeftAnchor[0]/PartyViewNonVR[0]/RootStack[0]/TurnViews[0]/PartyPieceView1[1]/Offset[0]/Portrait[1]/PieceHealthBar2D[0]/HealthText[3]",
    name: "HealthText"
  },
  hud_hp_bar_1: {
    goId: "-64144",
    scene: "DontDestroyOnLoad",
    path: "/ScreenspaceHudViewNonVR%28Clone%29[0]/UICanvas[0]/LeftStretchAnchor[0]/LeftStretchOffset[0]/TopLeftAnchor[0]/PartyViewNonVR[0]/RootStack[0]/TurnViews[0]/PartyPieceView1[1]/Offset[0]/Portrait[1]/PieceHealthBar2D[0]",
    name: "PieceHealthBar2D"
  },
  hud_party_turn_1: {
    goId: "-64094",
    scene: "DontDestroyOnLoad",
    path: "/ScreenspaceHudViewNonVR%28Clone%29[0]/UICanvas[0]/LeftStretchAnchor[0]/LeftStretchOffset[0]/TopLeftAnchor[0]/PartyViewNonVR[0]/RootStack[0]/TurnViews[0]/PartyPieceView1[1]",
    name: "PartyPieceView1"
  }
};

export const VARIABLES = {
  stat_hp_max: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.Health.maxValue",
    val: "23",
    desc: "Hit Point Maximum (System.Single): Changes the hero's maximum HP stat via CameraTargetCharacter.targetPiece.stats.Health.maxValue."
  },
  stat_hp_cur: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.Health.value",
    val: "23",
    desc: "Current Hit Points (System.Single): Sets the hero's current HP value (heals or damages the hero)."
  },
  stat_phys_armor: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.PhysicalArmor.value",
    val: "10",
    desc: "Physical Armor Current (System.Single): Current physical armor points absorbing incoming physical damage."
  },
  stat_phys_armor_max: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.PhysicalArmor.maxValue",
    val: "10",
    desc: "Physical Armor Max (System.Single): Maximum physical armor cap for this hero."
  },
  stat_magic_armor: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.MagicArmor.value",
    val: "10",
    desc: "Magic Armor Current (System.Single): Current magic armor points absorbing incoming spell/magic damage."
  },
  stat_magic_armor_max: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.MagicArmor.maxValue",
    val: "10",
    desc: "Magic Armor Max (System.Single): Maximum magic armor cap for this hero."
  },
  stat_downed_counter: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.DownedCounter.value",
    val: "0",
    desc: "Downed Counter (System.Single): Number of turns or times the hero has been downed."
  },
  stat_downed_max: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.DownedCounter.maxValue",
    val: "5",
    desc: "Max Downed Counter (System.Single): Maximum downed threshold before permanent knock-out."
  },
  stat_ap_cur: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.ActionPoints.value",
    val: "4",
    desc: "Current Action Points Stat (System.Single): Available action points for the current turn."
  },
  stat_ap_max: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.ActionPoints.maxValue",
    val: "4",
    desc: "Max Action Points Stat (System.Single): Maximum action points capacity."
  },
  piece_ap_per_turn: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.actionPointsPerTurn",
    val: "4",
    desc: "Action Points Per Turn (System.Int32): Number of action points replenished at the start of each turn."
  },
  stat_move_range: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.MoveRange.value",
    val: "8",
    desc: "Move Range (System.Single): How many board tiles the hero can move per action."
  },
  stat_move_range_max: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.MoveRange.maxValue",
    val: "8",
    desc: "Max Move Range (System.Single): Maximum movement range stat."
  },
  stat_attack_dmg: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.AttackDamage.value",
    val: "15",
    desc: "Base Attack Damage (System.Single): Base melee/ranged attack damage stat."
  },
  stat_crit_chance: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.CritChance.value",
    val: "1.0",
    desc: "Critical Hit Chance (System.Single): Probability modifier for critical hits."
  },
  stat_hit_chance: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.HitChance.value",
    val: "1.0",
    desc: "Hit Chance / Accuracy (System.Single): Base accuracy stat for attack rolls."
  },
  stat_vision_range: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.VisionRange.value",
    val: "12",
    desc: "Vision Range (System.Single): Line-of-sight tile distance revealed by this piece."
  },
  stat_inv_capacity: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.stats.InventoryCapacity.value",
    val: "10",
    desc: "Card Inventory Capacity (System.Single): Maximum cards the hero can hold in hand/inventory."
  },
  piece_cards_played: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.cardsPlayedThisTurn",
    val: "0",
    desc: "Cards Played This Turn (System.Int32): Reset to 0 to clear per-turn card play counters."
  },
  piece_xp_award: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.awardedExperienceWhenKilled",
    val: "500",
    desc: "XP Awarded When Killed (System.Int32): Experience granted to the party when this piece is defeated."
  },
  piece_loot_chance: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.lootChanceOverride",
    val: "1.0",
    desc: "Loot Chance Override (System.Single): Override drop probability when defeated (-1 = default, 1.0 = 100%)."
  },
  piece_always_visible: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.alwaysVisible",
    val: "true",
    desc: "Always Visible (System.Boolean): Makes the unit visible regardless of fog-of-war or line-of-sight."
  },
  piece_is_downable: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.isDownable",
    val: "true",
    desc: "Can Be Downed (System.Boolean): Whether lethal damage puts the piece into a downed state instead of instant removal."
  },
  piece_is_shovable: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.isShovable",
    val: "false",
    desc: "Can Be Shoved (System.Boolean): Set to false to make the hero immune to knockback/shove effects."
  },
  piece_is_player_ctrl: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.isPlayerControlled",
    val: "true",
    desc: "Is Player Controlled (System.Boolean): Whether players can control this piece on its turn."
  },
  piece_is_elite: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.isElite",
    val: "true",
    desc: "Is Elite Unit (System.Boolean): Toggles Elite status badge and rules for the piece."
  },
  piece_uses_health: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.usesHealth",
    val: "true",
    desc: "Uses Health System (System.Boolean): Whether the piece takes damage and tracks health."
  },
  piece_blocks_vision: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.blocksVision",
    val: "false",
    desc: "Blocks Line of Sight (System.Boolean): Whether this piece blocks vision rays across the board."
  },
  piece_orientation: {
    comp: "Jade.GameView.CameraTargetCharacter",
    member: "targetPiece.orientation",
    val: "90",
    desc: "Board Orientation Angle (System.Single): Facing angle in degrees on the grid."
  },
  view_model_index: {
    comp: "Jade.GameView.PieceComponents",
    member: "ActiveModelIndex",
    val: "2",
    desc: "Active 3D Pose/Model Index (System.Int32): Switches the active pose mesh index on the miniature."
  },
  view_notif_margin: {
    comp: "Jade.GameView.PieceComponents",
    member: "notificationAnchorMarginAboveCharacter",
    val: "0.2",
    desc: "Notification Anchor Height Margin (System.Single): Vertical offset of floating 3D status/health above miniature."
  },
  view_drag_mask: {
    comp: "Jade.GameView.PieceView",
    member: "PreferredDragLayerMask",
    val: "1007361",
    desc: "Preferred Drag Layer Mask (System.Int32): Physics raycast layer mask used when dragging the piece."
  },
  view_rot_y: {
    comp: "Jade.GameView.PieceView",
    member: "previousNonGrabbedRotationY",
    val: "90",
    desc: "Previous Non-Grabbed Rotation Y (System.Single): Resting Y-axis rotation of the 3D miniature."
  },
  phys_position: {
    comp: "UnityEngine.Rigidbody",
    member: "position",
    val: "(-7.50, 2.46, 2.50)",
    desc: "3D World Position (UnityEngine.Vector3): Direct world coordinates of the piece's Rigidbody."
  },
  phys_mass: {
    comp: "UnityEngine.Rigidbody",
    member: "mass",
    val: "1",
    desc: "Rigidbody Mass (System.Single): Physics mass of the board piece."
  },
  grab_throw_vel: {
    comp: "Jade.GameView.JadeGrabbable",
    member: "throwVelocityScale",
    val: "1.5",
    desc: "Grab Throw Velocity Scale (System.Single): Multiplier for release velocity when throwing a grabbed piece."
  },
  grab_smooth_pos: {
    comp: "Jade.GameView.JadeGrabbable",
    member: "smoothPositionAmount",
    val: "12",
    desc: "Grab Smooth Position Speed (System.Single): Interpolation responsiveness while dragging a piece."
  },
  grab_max_scale: {
    comp: "UnityEngine.XR.Interaction.Toolkit.Transformers.XRGeneralGrabTransformer",
    member: "maximumScaleRatio",
    val: "3.0",
    desc: "Max Two-Handed Scale Ratio (System.Single): Maximum zoom/scale ratio when scaling with grab transformer."
  },
  grab_min_scale: {
    comp: "UnityEngine.XR.Interaction.Toolkit.Transformers.XRGeneralGrabTransformer",
    member: "minimumScaleRatio",
    val: "0.25",
    desc: "Min Two-Handed Scale Ratio (System.Single): Minimum zoom/scale ratio."
  },
  cam_card_dist: {
    comp: "Jade.GameView.Prototyping.AvatarNonVR",
    member: "cardHandPreferredDistance",
    val: "12",
    desc: "Card Hand Preferred Distance (System.Single): Distance from the camera to the 3D card hand in Non-VR mode.",
    forceEntity: "avatar_nonvr"
  },
  cam_fov_card_dist: {
    comp: "Jade.GameView.Prototyping.AvatarNonVR",
    member: "fovCardHandDistance",
    val: "6.65",
    desc: "FOV Card Hand Distance (System.Single): Field-of-view adjusted card hand distance.",
    forceEntity: "avatar_nonvr"
  },
  cam_fov_card_height: {
    comp: "Jade.GameView.Prototyping.AvatarNonVR",
    member: "fovInitialCardHandHeight",
    val: "7.68",
    desc: "FOV Initial Card Hand Height (System.Single): Vertical placement of the 3D card hand in the viewport.",
    forceEntity: "avatar_nonvr"
  },
  cam_fov_init_dist: {
    comp: "Jade.GameView.Prototyping.AvatarNonVR",
    member: "fovInitialCameraDistance",
    val: "1.0",
    desc: "FOV Initial Camera Distance (System.Single): Baseline camera distance offset.",
    forceEntity: "avatar_nonvr"
  },
  cam_fov_init_height: {
    comp: "Jade.GameView.Prototyping.AvatarNonVR",
    member: "fovInitialHeight",
    val: "0.64",
    desc: "FOV Initial Height (System.Single): Baseline camera height above the table.",
    forceEntity: "avatar_nonvr"
  },
  cam_fov_zoom_comp: {
    comp: "Jade.GameView.Prototyping.AvatarNonVR",
    member: "fovZoomCompensationFactor",
    val: "0.2",
    desc: "FOV Zoom Compensation Factor (System.Single): How strongly FOV changes compensate camera zoom.",
    forceEntity: "avatar_nonvr"
  },
  net_avatar_scale: {
    comp: "Jade.GameView.HideWhenViewedFromBehind",
    member: "avatarScale",
    val: "40",
    desc: "Network Avatar Scale (System.Single): Scale factor of the table-side XR avatar.",
    forceEntity: "network_avatar"
  },
  net_hide_dist: {
    comp: "Jade.GameView.HideWhenViewedFromBehind",
    member: "hideDistance",
    val: "1.0",
    desc: "Network Avatar Hide Distance (System.Single): Distance threshold at which the avatar fades out when camera is close.",
    forceEntity: "network_avatar"
  },
  hud_hp_text: {
    comp: "TMPro.TextMeshProUGUI",
    member: "text",
    val: "23 / 23 ",
    desc: "HUD Health Bar Text String (System.String): Text displayed inside the top-left party portrait health bar.",
    forceEntity: "hud_hp_text_1"
  },
  hud_hp_width_max: {
    comp: "Jade.GameView.PieceHealthBarView",
    member: "healthBarWidthMax",
    val: "360",
    desc: "HUD Health Bar Max Pixel Width (System.Single): Maximum UI width of the 2D health bar.",
    forceEntity: "hud_hp_bar_1"
  },
  hud_hp_width_min: {
    comp: "Jade.GameView.PieceHealthBarView",
    member: "healthBarWidthMin",
    val: "26",
    desc: "HUD Health Bar Min Pixel Width (System.Single): Minimum UI width of the 2D health bar.",
    forceEntity: "hud_hp_bar_1"
  },
  hud_turn_scale: {
    comp: "Jade.GameView.UI.PartyTurnView",
    member: "scaleWhenSelectedFactor",
    val: "1.35",
    desc: "Active Turn Portrait Scale Factor (System.Single): How much the active hero's portrait enlarges on their turn.",
    forceEntity: "hud_party_turn_1"
  },
  hud_turn_duration: {
    comp: "Jade.GameView.UI.PartyTurnView",
    member: "scaleDuration",
    val: "0.3",
    desc: "Active Turn Scale Transition Duration (System.Single): Animation duration in seconds for turn portrait scaling.",
    forceEntity: "hud_party_turn_1"
  }
};

export function syncSelectorJson() {
  const entityKey = document.getElementById("mcp-target-entity").value;
  const e = ENTITIES[entityKey];
  const comp = document.getElementById("mcp-comp-name").value;
  if (!e) return;
  document.getElementById("mcp-go-id").value = e.goId;
  document.getElementById("mcp-search-name").value = e.name.replace("(Clone)", "");
  document.getElementById("mcp-search-comp").value = comp;
  document.getElementById("mcp-selector").value = JSON.stringify({
    scene: e.scene,
    path: e.path,
    name: e.name,
    component: comp
  });
}

export function applyTargetEntity() {
  syncSelectorJson();
}

export function applyVariableSelection() {
  const varKey = document.getElementById("mcp-variable-select").value;
  const v = VARIABLES[varKey];
  if (!v) return;
  if (v.forceEntity) {
    document.getElementById("mcp-target-entity").value = v.forceEntity;
  } else {
    const curEntity = document.getElementById("mcp-target-entity").value;
    if (["avatar_nonvr", "network_avatar", "player_input", "hud_hp_text_1", "hud_hp_bar_1", "hud_party_turn_1"].includes(curEntity)) {
      document.getElementById("mcp-target-entity").value = "paladin_piece";
    }
  }
  document.getElementById("mcp-comp-name").value = v.comp;
  document.getElementById("mcp-member-name").value = v.member;
  document.getElementById("mcp-member-value").value = v.val;
  document.getElementById("var-description").innerHTML = `<strong>${v.member}:</strong> ${v.desc}`;
  document.getElementById("mcp-profile-name").value = `battlemarked_${varKey}`;
  syncSelectorJson();
}

async function _executeFingerprint() {
  const out = document.getElementById("fp-output");
  if (out) {
    out.style.display = "block";
    out.textContent = "Running fingerprint...";
  }
  const game_dir = document.getElementById("fp-game-dir").value;
  const res = await apiPost("/api/fingerprint", { game_dir });
  const data = res?.data ?? res;
  if (out) {
    out.textContent = JSON.stringify(data, null, 2);
  }
  await refreshGamesList(false);
  return data;
}

export async function runFingerprint() {
  const btn = _resolveMcpBtn("");
  try {
    await runTrackedAction(btn, _executeFingerprint, {
      pendingText: "Fingerprinting game directory...",
      successText: "Fingerprint complete",
    });
  } catch (err) {
    const out = document.getElementById("fp-output");
    if (out) out.textContent = `Fingerprint failed: ${err?.message || err}`;
  }
}

export async function runFingerprintAndRescan() {
  const btn = _resolveMcpBtn("btn-fp-rescan");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const fp = await _executeFingerprint();
        await rescanActiveGameMetadata();
        return fp;
      },
      {
        pendingText: "Fingerprinting and rescanning active game metadata...",
        successText: "Fingerprint & metadata rescan complete",
      },
    );
  } catch (err) {
    const out = document.getElementById("fp-output");
    if (out) out.textContent = `Fingerprint & rescan failed: ${err?.message || err}`;
  }
}

export async function runProvision() {
  const btn = _resolveMcpBtn("");
  const out = document.getElementById("prov-output");
  if (out) out.textContent = "Provisioning BepInEx 6 IL2CPP + BepInExMCP.IL2CPP.dll...";
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/provision", {
          game_dir: document.getElementById("fp-game-dir").value,
          bepinex_zip_path: document.getElementById("prov-zip-path").value,
          expected_sha256: document.getElementById("prov-sha256").value,
          plugin_build_dir: document.getElementById("prov-plugin-dir").value,
        });
        const data = res?.data ?? res;
        if (out) out.textContent = JSON.stringify(data, null, 2);
        await refreshGamesList(false);
        return data;
      },
      {
        pendingText: "Provisioning BepInEx + bridge plugin (with backup)...",
        successText: "Provision & backup complete",
      },
    );
  } catch (err) {
    if (out) out.textContent = `Provision failed: ${err?.message || err}`;
  }
}

export async function runRestore() {
  const btn = _resolveMcpBtn("");
  const out = document.getElementById("prov-output");
  if (out) out.textContent = "Running Clean Restore...";
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/restore", {
          game_dir: document.getElementById("fp-game-dir").value,
        });
        const data = res?.data ?? res;
        if (out) out.textContent = JSON.stringify(data, null, 2);
        await refreshGamesList(false);
        return data;
      },
      {
        pendingText: "Running Clean Restore from manifest/backup...",
        successText: "Clean Restore complete",
      },
    );
  } catch (err) {
    if (out) out.textContent = `Clean Restore failed: ${err?.message || err}`;
  }
}

export function baseMcpPayload(action, argumentsObj) {
  return {
    action,
    arguments: argumentsObj,
    python_path: document.getElementById("mcp-python").value,
    mcp_app_path: document.getElementById("mcp-app").value,
    game_ip: "localhost"
  };
}

export function parseSelector() {
  try {
    return JSON.parse(document.getElementById("mcp-selector").value);
  } catch (e) {
    return null;
  }
}

export async function mcpTestBridge() {
  const btn = _resolveMcpBtn("");
  const out = document.getElementById("mcp-output");
  if (out) out.textContent = "Calling ping_unity_bridge + get_runtime_capabilities over persistent MCP stdio...";
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/mcp/call", baseMcpPayload("test_bridge", {}));
        const data = res?.data ?? res;
        if (out) out.textContent = JSON.stringify(data, null, 2);
        return data;
      },
      {
        pendingText: "Testing persistent MCP stdio bridge...",
        successText: "MCP bridge test succeeded",
      },
    );
  } catch (err) {
    if (out) out.textContent = `MCP test failed: ${err?.message || err}`;
  }
}

export async function mcpSearch(mode) {
  const btn = _resolveMcpBtn("");
  const out = document.getElementById("mcp-output");
  if (out) out.textContent = `Running ${mode}...`;
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/mcp/call", baseMcpPayload("search", {
          mode,
          name: document.getElementById("mcp-search-name").value,
          component_name: document.getElementById("mcp-search-comp").value,
          include_inactive: false,
          limit: 50,
        }));
        const data = res?.data ?? res;
        if (out) out.textContent = JSON.stringify(data, null, 2);
        return data;
      },
      {
        pendingText: `Running MCP ${mode}...`,
        successText: `MCP ${mode} complete`,
      },
    );
  } catch (err) {
    if (out) out.textContent = `MCP ${mode} failed: ${err?.message || err}`;
  }
}

export async function mcpInspect() {
  const btn = _resolveMcpBtn("");
  const out = document.getElementById("mcp-output");
  if (out) out.textContent = "Inspecting components and member...";
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/mcp/call", baseMcpPayload("inspect", {
          game_object_id: document.getElementById("mcp-go-id").value,
          selector: parseSelector(),
          component_name: document.getElementById("mcp-comp-name").value,
          member_name: document.getElementById("mcp-member-name").value,
        }));
        const data = res?.data ?? res;
        if (out) out.textContent = JSON.stringify(data, null, 2);
        return data;
      },
      {
        pendingText: "Inspecting Unity object & component member...",
        successText: "MCP inspect complete",
      },
    );
  } catch (err) {
    if (out) out.textContent = `MCP inspect failed: ${err?.message || err}`;
  }
}

export async function mcpEdit() {
  const btn = _resolveMcpBtn("");
  const out = document.getElementById("mcp-output");
  if (out) out.textContent = "Setting component value and verifying...";
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/mcp/call", baseMcpPayload("edit", {
          game_object_id: document.getElementById("mcp-go-id").value,
          selector: parseSelector(),
          component_name: document.getElementById("mcp-comp-name").value,
          member_name: document.getElementById("mcp-member-name").value,
          value: document.getElementById("mcp-member-value").value,
        }));
        const data = res?.data ?? res;
        if (out) out.textContent = JSON.stringify(data, null, 2);
        return data;
      },
      {
        pendingText: "Applying live component value via MCP...",
        successText: "MCP edit applied and verified",
      },
    );
  } catch (err) {
    if (out) out.textContent = `MCP edit failed: ${err?.message || err}`;
  }
}

export async function mcpProfile() {
  const btn = _resolveMcpBtn("");
  const out = document.getElementById("mcp-output");
  if (out) out.textContent = "Saving and applying mod profile...";
  try {
    await runTrackedAction(
      btn,
      async () => {
        const res = await apiPost("/api/mcp/call", baseMcpPayload("profile_save_apply", {
          profile_name: document.getElementById("mcp-profile-name").value,
          selector: parseSelector(),
          component_name: document.getElementById("mcp-comp-name").value,
          member_name: document.getElementById("mcp-member-name").value,
          value: document.getElementById("mcp-member-value").value,
        }));
        const data = res?.data ?? res;
        if (out) out.textContent = JSON.stringify(data, null, 2);
        return data;
      },
      {
        pendingText: "Saving and applying MCP mod profile...",
        successText: "MCP profile saved & applied",
      },
    );
  } catch (err) {
    if (out) out.textContent = `MCP profile failed: ${err?.message || err}`;
  }
}


let MOD_BUILDER_RULES = [];

export function _mbEsc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function syncModBuilderWithGame(game) {
  if (!game) return;
  const mi = game.modding_info || {};
  const labelEl = document.getElementById("mb-target-game-label");
  const badgeEl = document.getElementById("mb-compat-badge");
  const hintEl = document.getElementById("mb-compat-hint");
  const nameEl = document.getElementById("mb-mod-name");
  const idEl = document.getElementById("mb-mod-id");

  if (labelEl) labelEl.textContent = game.name || game.id || "Active Game";

  const isUnreal = (mi.engine || "").includes("Unreal") || game.runtime === "Unreal";
  const isIl2Cpp = (mi.scripting_backend || "").includes("IL2CPP") || game.runtime === "Unity_IL2CPP";
  const isMono = (mi.scripting_backend || "").includes("Mono") || game.runtime === "Unity_Mono";

  if (badgeEl) {
    if (isIl2Cpp) {
      badgeEl.className = "pill yes";
      badgeEl.textContent = "BepInEx 6 Unity.IL2CPP";
    } else if (isUnreal) {
      badgeEl.className = "pill yes";
      badgeEl.textContent = "Unreal RE-UE4SS + INI";
    } else if (isMono) {
      badgeEl.className = "pill warn";
      badgeEl.textContent = "Unity Mono Data / BepInEx";
    } else {
      badgeEl.className = "pill warn";
      badgeEl.textContent = mi.recommended_mcp_stack || game.runtime || "Data Patch";
    }
  }

  if (hintEl) {
    if (isIl2Cpp) {
      hintEl.innerHTML = `Compiles a standalone <code>net6.0</code> BepInEx 6 IL2CPP plugin <code>.dll</code> directly against <code>BepInEx\\core\\*.dll</code> and <code>BepInEx\\interop\\*.dll</code> for <strong>${_mbEsc(game.name || game.id)}</strong>, packaged into a ready-to-install <code>.zip</code>.`;
    } else if (isUnreal) {
      hintEl.innerHTML = `Generates an Unreal Engine RE-UE4SS Lua script (<code>main.lua</code>) + <code>GameUserSettings_Override.ini</code> for <strong>${_mbEsc(game.name || game.id)}</strong>, packaged into a ready-to-install <code>.zip</code>.`;
    } else {
      hintEl.innerHTML = `Generates a structured JSON data patch + manifest and installation instructions for <strong>${_mbEsc(game.name || game.id)}</strong>.`;
    }
  }

  const gameChanged = state.lastMbGameId !== game.id;
  if (gameChanged) {
    state.lastMbGameId = game.id;
    state.lastBuiltMod = null;
    const resultPanel = document.getElementById("mb-result-panel");
    if (resultPanel) resultPanel.style.display = "none";

    const slug = (game.id || "game").replace(/[^a-zA-Z0-9]+/g, "").toLowerCase() || "game";
    if (nameEl) nameEl.value = `${game.name || game.id} Custom Mod`;
    if (idEl) idEl.value = `com.modtool.${slug}.custommod`;

    if (isUnreal) {
      MOD_BUILDER_RULES = [
        {
          label: "Resolution Scale Override",
          entity_name: "GameUserSettings",
          component_name: "ScalabilityGroups",
          member_path: "sg.ResolutionQuality",
          value: "100"
        }
      ];
    } else {
      MOD_BUILDER_RULES = [
        {
          label: "Custom Stat Edit",
          entity_name: "CameraHolder(Clone)",
          component_name: game.default_component || "Jade.GameView.CameraTargetCharacter",
          member_path: game.default_member || "targetPiece.stats.Health.maxValue",
          value: game.default_value || "23"
        }
      ];
    }
    renderModBuilderRules();
  }
}

export function renderModBuilderRules() {
  const tbody = document.getElementById("mb-rules-tbody");
  const badge = document.getElementById("modbuilder-rules-badge");
  if (badge) badge.textContent = String(MOD_BUILDER_RULES.length);
  if (!tbody) return;

  if (!MOD_BUILDER_RULES.length) {
    tbody.innerHTML = `<tr><td colspan="6" style="color:var(--muted);">No edit rules added yet. Click "Import Current Live MCP Edit" or "Add Custom Rule".</td></tr>`;
    return;
  }

  tbody.innerHTML = MOD_BUILDER_RULES.map((rule, idx) => `
    <tr>
      <td><input type="text" value="${_mbEsc(rule.label)}" oninput="updateModRuleField(${idx}, 'label', this.value)" /></td>
      <td><input type="text" value="${_mbEsc(rule.entity_name)}" oninput="updateModRuleField(${idx}, 'entity_name', this.value)" /></td>
      <td><input type="text" value="${_mbEsc(rule.component_name)}" oninput="updateModRuleField(${idx}, 'component_name', this.value)" /></td>
      <td><input type="text" value="${_mbEsc(rule.member_path)}" oninput="updateModRuleField(${idx}, 'member_path', this.value)" /></td>
      <td><input type="text" value="${_mbEsc(rule.value)}" oninput="updateModRuleField(${idx}, 'value', this.value)" /></td>
      <td><button class="danger btn-sm" onclick="removeModRule(${idx})">Remove</button></td>
    </tr>
  `).join("");
}

export function updateModRuleField(index, field, value) {
  if (MOD_BUILDER_RULES[index]) {
    MOD_BUILDER_RULES[index][field] = value;
  }
}

export function addCurrentMcpEditToMod() {
  const entitySel = document.getElementById("mcp-target-entity");
  const entityLabel = entitySel && entitySel.selectedOptions.length
    ? entitySel.selectedOptions[0].textContent.split("(")[0].trim()
    : "CameraHolder(Clone)";
  const compName = (document.getElementById("mcp-comp-name")?.value || "").trim() || "Jade.GameView.CameraTargetCharacter";
  const memberPath = (document.getElementById("mcp-member-name")?.value || "").trim() || "targetPiece.stats.Health.maxValue";
  const val = (document.getElementById("mcp-member-value")?.value || "").trim() || "23";

  MOD_BUILDER_RULES.push({
    label: `${entityLabel} (${memberPath.split(".").slice(-2).join(".")})`,
    entity_name: entityLabel,
    component_name: compName,
    member_path: memberPath,
    value: val,
  });
  renderModBuilderRules();
}

export function addBlankModRule() {
  MOD_BUILDER_RULES.push({
    label: `Rule ${MOD_BUILDER_RULES.length + 1}`,
    entity_name: "CameraHolder(Clone)",
    component_name: "",
    member_path: "",
    value: "",
  });
  renderModBuilderRules();
}

export function removeModRule(index) {
  MOD_BUILDER_RULES.splice(index, 1);
  renderModBuilderRules();
}

export async function buildCompatibleMod() {
  const btn = _resolveMcpBtn("btn-build-mod");
  const resultPanel = document.getElementById("mb-result-panel");
  const logPre = document.getElementById("mb-build-log");
  if (!MOD_BUILDER_RULES.length) {
    alert("Add at least one game edit rule before building a mod.");
    return;
  }

  try {
    await runTrackedAction(
      btn,
      async () => {
        const game = state.managedGames.find(g => g.id === state.activeGameId) || state.lastRenderedGame || {};
        const payload = {
          game_id: state.activeGameId,
          game_dir: game.game_dir || document.getElementById("gm-game-dir")?.value || "",
          mod_name: document.getElementById("mb-mod-name")?.value || "CustomMod",
          mod_id: document.getElementById("mb-mod-id")?.value || "com.modtool.custommod",
          mod_version: document.getElementById("mb-mod-version")?.value || "1.0.0",
          author: document.getElementById("mb-author")?.value || "The Modding Tool",
          apply_interval_sec: parseFloat(document.getElementById("mb-interval")?.value || "2.0") || 2.0,
          edits: MOD_BUILDER_RULES,
        };

        const data = await apiPost("/api/mods/build", payload);
        state.lastBuiltMod = data;

        if (resultPanel) resultPanel.style.display = "block";
        document.getElementById("mb-out-kind").textContent = data.mod_kind || "Standalone Mod";
        document.getElementById("mb-out-zip").textContent = data.zip_path || "—";
        document.getElementById("mb-out-compat").textContent = data.compatibility || "Verified";
        document.getElementById("mb-install-instructions").textContent = data.instructions_text || (data.install_steps || []).join("\n");
        if (logPre) {
          logPre.textContent = `${data.summary || "Build succeeded."}\nSHA-256: ${data.zip_sha256 || "—"}\n${data.compile_output ? "\nCompiler Output:\n" + data.compile_output : ""}`;
        }
        loadLogs();
        return data;
      },
      {
        pendingText: "Compiling & packaging compatible mod...",
        successText: (d) => d?.summary || "Mod build succeeded",
      },
    );
  } catch (err) {
    if (resultPanel) resultPanel.style.display = "block";
    if (logPre) logPre.textContent = `Build failed: ${err?.message || err}`;
  }
}

export async function installBuiltModToGame() {
  if (!state.lastBuiltMod || !state.lastBuiltMod.mod_slug) {
    alert("Build a mod first before installing.");
    return;
  }
  const btn = _resolveMcpBtn("btn-install-built-mod");
  const logPre = document.getElementById("mb-build-log");
  try {
    await runTrackedAction(
      btn,
      async () => {
        const game = state.managedGames.find(g => g.id === state.activeGameId) || state.lastRenderedGame || {};
        const data = await apiPost("/api/mods/install", {
          mod_slug: state.lastBuiltMod.mod_slug,
          game_id: state.activeGameId,
          game_dir: game.game_dir || document.getElementById("gm-game-dir")?.value || "",
        });
        if (logPre) {
          logPre.textContent = `${data.summary}\nInstalled files:\n- ${(data.installed_files || []).join("\n- ")}`;
        }
        loadLogs();
        return data;
      },
      {
        pendingText: "Installing built mod into game directory...",
        successText: (d) => d?.summary || "Built mod installed",
      },
    );
  } catch (err) {
    if (logPre) logPre.textContent = `Install failed: ${err?.message || err}`;
  }
}

export async function openBuiltModFolder() {
  if (!state.lastBuiltMod || !state.lastBuiltMod.output_dir) return;
  try {
    await apiPost("/api/tools/open-folder", { path: state.lastBuiltMod.output_dir });
  } catch (err) {
    alert(`Failed to open folder: ${err.message || err}`);
  }
}


export function addHarmonyHookRule() {
  MOD_BUILDER_RULES.push({
    label: `Harmony Hook ${MOD_BUILDER_RULES.length + 1}`,
    rule_type: "harmony_patch",
    entity_name: "Harmony Hook",
    component_name: "Jade.GameView.CameraTargetCharacter",
    member_path: "Awake",
    target_method: "Awake",
    hook_kind: "Postfix",
    skip_original: false,
    value: "Postfix",
  });
  renderModBuilderRules();
}
