const EFFECTS = [
  [0, "Rainbow", "Радуга"],
  [1, "Breathing", "Дыхание"],
  [2, "Chase", "Погоня"],
  [3, "Meteor", "Метеор"],
  [4, "Sparkle", "Искры"],
  [5, "Gradient", "Градиент"],
  [6, "Marquee", "Бегущий"],
];
const MUSIC = [
  [0, "Wave", "Волна"],
  [1, "Pulse", "Пульс"],
  [2, "Spectrum", "Спектр"],
  [3, "Flash", "Вспышки"],
  [4, "Gradient", "Градиент"],
  [5, "Chase", "Погоня"],
  [6, "Rainbow", "Радуга"],
];
const COLORS = {
  warm: "#ffb36b",
  cool: "#9cc7ff",
  white: "#f4f7ff",
  red: "#ff4d4d",
  green: "#3dde7a",
  blue: "#4d8dff",
  purple: "#b46bff",
};
const LABELS = {
  all: "Все",
  usb: "Монитор",
  ceiling: "Потолок",
  floor: "Пол",
  bt: "Комната",
  bulb: "Лампа",
};
const KIND_LABEL = { auto: "Авто", circadian: "Сутки", activity: "Режим", template: "Шаблон" };

const statusLine = document.getElementById("statusLine");
const preview = document.getElementById("preview");
const bri = document.getElementById("brightness");
const spd = document.getElementById("speed");
const sens = document.getElementById("sensitivity");
const briVal = document.getElementById("briVal");
const spdVal = document.getElementById("spdVal");
const sensVal = document.getElementById("sensVal");
const sensRow = document.getElementById("sensRow");
const ambiBtn = document.getElementById("btnAmbi");
const autostart = document.getElementById("autostart");
const mirrorRoom = document.getElementById("mirrorRoom");
const ambiMonitor = document.getElementById("ambiMonitor");
const btBadge = document.getElementById("btBadge");
const btList = document.getElementById("btList");
const btHint = document.getElementById("btHint");
const btCeiling = document.getElementById("btCeiling");
const btFloor = document.getElementById("btFloor");
const btCeilingState = document.getElementById("btCeilingState");
const btFloorState = document.getElementById("btFloorState");
const verBadge = document.getElementById("verBadge");
const aiGrid = document.getElementById("aiGrid");
const aiMeta = document.getElementById("aiMeta");

let ambiOn = false;
let target = "all";
let selected = null;
let btStatus = { ceiling: {}, floor: {}, scan: [] };
let bulbStatus = {};
let aiScene = "";
let aiScenesCache = [];

function bridge(payload) {
  const body = Object.assign({ target }, payload);
  if (window.webkit?.messageHandlers?.synclight) {
    window.webkit.messageHandlers.synclight.postMessage(body);
  }
}

function setStatus(text, mode) {
  statusLine.textContent = text;
  preview.classList.remove("off", "static", "ambi");
  if (mode) preview.classList.add(mode);
}

function speedLabel(v) {
  if (v < 25) return "Медленно";
  if (v < 45) return "Спокойно";
  if (v < 65) return "Норм";
  if (v < 85) return "Быстро";
  return "Макс";
}
function briLabel(v) {
  return `${Math.round((v / 255) * 100)}%`;
}

function clearChips() {
  document.querySelectorAll(".chip.on, .ai-card.on").forEach((el) => el.classList.remove("on"));
}

function fillGrid(id, items, kind) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = "";
  items.forEach(([idx, name, desc]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.innerHTML = `<span>${name}</span><small>${desc}</small>`;
    btn.onclick = () => {
      clearChips();
      btn.classList.add("on");
      selected = kind;
      ambiOn = false;
      ambiBtn.classList.remove("on");
      sensRow.hidden = kind !== "sound";
      bridge({
        action: kind,
        index: idx,
        brightness: +bri.value,
        speed: +spd.value,
        sensitivity: +sens.value,
      });
      setStatus(`${LABELS[target]} · ${name}`, "static");
    };
    el.appendChild(btn);
  });
}

function fillColors() {
  const el = document.getElementById("panel-colors");
  el.innerHTML = "";
  Object.entries(COLORS).forEach(([name, hex]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip";
    btn.innerHTML = `<span class="swatch" style="background:${hex}"></span><span>${name}</span>`;
    btn.onclick = () => {
      clearChips();
      btn.classList.add("on");
      selected = "color";
      ambiOn = false;
      ambiBtn.classList.remove("on");
      sensRow.hidden = true;
      bridge({ action: "color", name, brightness: +bri.value });
      setStatus(`${LABELS[target]} · ${name}`, "static");
      preview.querySelector("i").style.background = hex;
    };
    el.appendChild(btn);
  });
}

function swatchFor(scene) {
  if (scene.color && scene.color.length === 3) {
    const [r, g, b] = scene.color;
    return `rgb(${r},${g},${b})`;
  }
  if (scene.id === "circadian") return "linear-gradient(90deg,#9cc7ff,#ffb36b,#402070)";
  if (scene.id === "auto") return "linear-gradient(90deg,#5aa7ff,#7d5cff,#ff6b9a)";
  return "linear-gradient(90deg,#5aa7ff,#ffb36b)";
}

function renderAi(scenes, activeId) {
  if (!aiGrid) return;
  if (Array.isArray(scenes) && scenes.length) aiScenesCache = scenes;
  const list = aiScenesCache;
  if (!list.length) {
    aiGrid.innerHTML = "<p class='note'>Сцены загружаются…</p>";
    return;
  }
  aiGrid.innerHTML = "";
  list.forEach((s) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ai-card" + (s.id === activeId ? " on" : "");
    btn.innerHTML = `
      <span class="kind">${KIND_LABEL[s.kind] || s.kind}</span>
      <span class="swatch" style="background:${swatchFor(s)}"></span>
      <strong>${s.title}</strong>
      <small>${s.blurb}</small>`;
    btn.onclick = () => {
      aiScene = s.id;
      document.querySelectorAll(".ai-card").forEach((c) => c.classList.remove("on"));
      btn.classList.add("on");
      clearChips();
      ambiOn = false;
      ambiBtn.classList.remove("on");
      bridge({ action: "ai_scene", id: s.id });
      setStatus(`Сцена · ${s.title}`, s.kind === "auto" ? "ambi" : "static");
    };
    aiGrid.appendChild(btn);
  });
}

function setTarget(next) {
  target = next;
  document.querySelectorAll(".zone").forEach((z) => {
    z.classList.toggle("on", z.dataset.target === target);
  });
  bridge({ action: "set_target", target });
  setStatus(`${LABELS[target]}`, "static");
}

function renderBulb(st) {
  bulbStatus = st || bulbStatus || {};
  const nameEl = document.getElementById("bulbName");
  const ipEl = document.getElementById("bulbIp");
  const idEl = document.getElementById("bulbId");
  const keyEl = document.getElementById("bulbKey");
  if (nameEl && !nameEl.value && bulbStatus.name) nameEl.value = bulbStatus.name;
  if (ipEl && bulbStatus.ip) ipEl.value = bulbStatus.ip;
  if (idEl && bulbStatus.device_id) idEl.value = bulbStatus.device_id;
  const label = document.getElementById("bulbStateLabel");
  const em = document.getElementById("bulbState");
  const hint = document.getElementById("bulbHint");
  if (label) {
    label.textContent =
      bulbStatus.name || bulbStatus.ip || "не настроена";
  }
  if (em) {
    em.textContent = bulbStatus.connected
      ? "online"
      : bulbStatus.configured
        ? "offline"
        : bulbStatus.has_key
          ? ""
          : "нет ключа";
    em.className = bulbStatus.connected ? "" : "off";
  }
  if (hint && bulbStatus.last_error) hint.textContent = bulbStatus.last_error;
  document.querySelectorAll(".zone").forEach((z) => {
    if (z.dataset.target === "bulb") {
      z.classList.toggle("missing", !bulbStatus.configured);
      z.classList.toggle("live", !!bulbStatus.connected);
    }
  });
}

function renderBt(st) {
  btStatus = st || btStatus;
  const ceil = btStatus.ceiling || {};
  const fl = btStatus.floor || {};
  btCeiling.textContent = ceil.name || ceil.address || "не назначен";
  btFloor.textContent = fl.name || fl.address || "не назначен";
  btCeilingState.textContent = ceil.connected ? "online" : ceil.bound ? "offline" : "";
  btFloorState.textContent = fl.connected ? "online" : fl.bound ? "offline" : "";
  btCeilingState.className = ceil.connected ? "" : "off";
  btFloorState.className = fl.connected ? "" : "off";

  const online = !!(ceil.connected || fl.connected);
  const bound = !!(ceil.bound || fl.bound);
  const bulbOn = !!(bulbStatus && bulbStatus.connected);
  btBadge.textContent = online || bulbOn
    ? "Комната · online"
    : bound || (bulbStatus && bulbStatus.bound)
      ? "Комната · offline"
      : "Комната";
  btBadge.className = "pill " + (online || bulbOn ? "ok" : bound ? "bad" : "soft");

  document.querySelectorAll(".zone").forEach((z) => {
    const t = z.dataset.target;
    if (t === "ceiling") {
      z.classList.toggle("missing", !ceil.bound);
      z.classList.toggle("live", !!ceil.connected);
    } else if (t === "floor") {
      z.classList.toggle("missing", !fl.bound);
      z.classList.toggle("live", !!fl.connected);
    } else if (t === "bt") {
      z.classList.toggle("missing", !(ceil.bound && fl.bound));
      z.classList.toggle("live", !!(ceil.connected && fl.connected));
    } else if (t === "all") {
      z.classList.toggle("live", !!(ceil.connected || fl.connected || bulbOn));
    }
  });

  const scan = btStatus.scan || [];
  btList.innerHTML = "";
  if (!scan.length) {
    if (btStatus.last_error) btHint.textContent = `Ошибка: ${btStatus.last_error}`;
    return;
  }
  btHint.textContent = `Найдено: ${scan.length}. Назначь роли или переименуй, затем «Подключить».`;
  scan.forEach((d) => {
    const row = document.createElement("div");
    row.className = "row";
    const title = d.name || "LED";
    row.innerHTML = `
      <div class="meta">
        <strong>${escapeHtml(title)}</strong>
        <small>${escapeHtml(d.protocol || "")} · ${d.rssi} dBm · ${escapeHtml(shortAddr(d.address))}</small>
      </div>
      <div class="acts">
        <button type="button" data-act="rename">Имя</button>
        <button type="button" data-role="ceiling">Потолок</button>
        <button type="button" data-role="floor">Пол</button>
      </div>`;
    row.querySelectorAll("button").forEach((btn) => {
      btn.onclick = () => {
        if (btn.dataset.act === "rename") {
          renameDevice(d.address, d.name || "");
          return;
        }
        bridge({
          action: "bt_assign",
          role: btn.dataset.role,
          address: d.address,
          name: d.name,
          protocol: d.protocol,
        });
      };
    });
    btList.appendChild(row);
  });
}

function escapeHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shortAddr(addr) {
  const a = String(addr || "");
  if (a.length <= 12) return a;
  return a.slice(0, 8) + "…" + a.slice(-4);
}

function renameDevice(address, current) {
  if (!address) return;
  const next = window.prompt("Имя устройства", current || "");
  if (next === null) return;
  bridge({ action: "bt_rename", address, label: String(next).trim() });
}

function renameBound(role) {
  const st = (role === "ceiling" ? btStatus.ceiling : btStatus.floor) || {};
  if (!st.address) {
    setStatus("Сначала назначь устройство", "static");
    return;
  }
  renameDevice(st.address, st.name || "");
}

fillColors();
fillGrid("panel-effects", EFFECTS, "effect");
fillGrid("panel-music", MUSIC, "sound");
renderAi([], "");

document.querySelectorAll(".zone").forEach((z) => {
  z.addEventListener("click", () => setTarget(z.dataset.target));
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("on"));
    tab.classList.add("on");
    const name = tab.dataset.tab;
    document.querySelectorAll(".panel").forEach((p) => {
      const on = p.id === `panel-${name}`;
      p.hidden = !on;
      p.classList.toggle("on", on);
    });
    sensRow.hidden = true;
  });
});

document.querySelectorAll("[data-cmd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const cmd = btn.dataset.cmd;
    clearChips();
    ambiOn = false;
    ambiBtn.classList.remove("on");
    sensRow.hidden = true;
    bridge({ action: cmd, brightness: +bri.value });
    setStatus(cmd === "on" ? `${LABELS[target]} · вкл` : `${LABELS[target]} · выкл`, cmd === "off" ? "off" : "static");
  });
});

ambiBtn.addEventListener("click", () => {
  ambiOn = !ambiOn;
  ambiBtn.classList.toggle("on", ambiOn);
  clearChips();
  sensRow.hidden = true;
  bridge({ action: "ambi", enabled: ambiOn, mirror: mirrorRoom.checked });
  setStatus(ambiOn ? "Ambilight" : "Готово", ambiOn ? "ambi" : "static");
});

function connectRoom(force) {
  setStatus(force ? "Переподключаю комнату…" : "Подключаю комнату…", "static");
  bridge({ action: "bt_connect", force: !!force });
}
document.getElementById("btnConnect").addEventListener("click", () => connectRoom(false));
document.getElementById("btnConnect").addEventListener("dblclick", () => connectRoom(true));
document.getElementById("btnScan").addEventListener("click", () => {
  btHint.textContent = "Сканирование…";
  bridge({ action: "bt_scan" });
});
document.getElementById("btnBindConnect").addEventListener("click", () => connectRoom(true));
document.getElementById("btnTestRgb").addEventListener("click", () => {
  setStatus("Тест цветов комнаты…", "static");
  bridge({ action: "bt_test_rgb" });
});
document.getElementById("btnBulbScan").addEventListener("click", () => {
  document.getElementById("bulbHint").textContent = "Сканирование Wi‑Fi…";
  bridge({ action: "bulb_scan" });
});
document.getElementById("btnBulbSave").addEventListener("click", () => {
  bridge({
    action: "bulb_save",
    name: document.getElementById("bulbName").value.trim(),
    ip: document.getElementById("bulbIp").value.trim(),
    device_id: document.getElementById("bulbId").value.trim(),
    local_key: document.getElementById("bulbKey").value.trim(),
    version: "3.3",
    enabled: true,
  });
});
document.getElementById("btnBulbTest").addEventListener("click", () => {
  bridge({ action: "bulb_test" });
});
document.getElementById("btnBulbCloud").addEventListener("click", () => {
  document.getElementById("bulbHint").textContent = "Запрос Local Key в Tuya Cloud…";
  bridge({ action: "bulb_cloud_key" });
});

document.getElementById("btnBtAdd").addEventListener("click", () => {
  const name = document.getElementById("btAddName").value.trim();
  const address = document.getElementById("btAddAddr").value.trim();
  const role = document.getElementById("btAddRole").value;
  if (!address) {
    setStatus("Укажи BLE-адрес", "static");
    return;
  }
  bridge({ action: "bt_add", role, address, name, protocol: "lednetwf" });
});
document.getElementById("btnRenameCeiling").addEventListener("click", () => renameBound("ceiling"));
document.getElementById("btnRenameFloor").addEventListener("click", () => renameBound("floor"));
document.getElementById("btnClearCeiling").addEventListener("click", () => {
  bridge({ action: "bt_clear", role: "ceiling" });
});
document.getElementById("btnClearFloor").addEventListener("click", () => {
  bridge({ action: "bt_clear", role: "floor" });
});

// Keep room badge honest while window is open
setInterval(() => bridge({ action: "bt_status" }), 4000);
document.getElementById("btnSwapRoles").addEventListener("click", () => {
  bridge({ action: "bt_swap" });
});
document.getElementById("btnAiStop").addEventListener("click", () => {
  aiScene = "";
  document.querySelectorAll(".ai-card").forEach((c) => c.classList.remove("on"));
  bridge({ action: "ai_stop" });
  setStatus("Сцены выкл", "static");
});

bri.addEventListener("input", () => { briVal.textContent = briLabel(+bri.value); });
spd.addEventListener("input", () => { spdVal.textContent = speedLabel(+spd.value); });
sens.addEventListener("input", () => { sensVal.textContent = sens.value; });
bri.addEventListener("change", () => bridge({ action: "brightness", value: +bri.value }));
spd.addEventListener("change", () => bridge({ action: "speed", value: +spd.value }));
sens.addEventListener("change", () => bridge({ action: "sensitivity", value: +sens.value }));
autostart.addEventListener("change", () => bridge({ action: "autostart", enabled: autostart.checked }));
mirrorRoom.addEventListener("change", () => bridge({ action: "mirror_room", enabled: mirrorRoom.checked }));
ambiMonitor.addEventListener("change", () => {
  bridge({ action: "ambi_monitor", index: +ambiMonitor.value || 1 });
});

function renderDisplays(list, selected) {
  if (!ambiMonitor) return;
  const items = Array.isArray(list) && list.length
    ? list
    : [{ index: 1, label: "Дисплей 1" }];
  const cur = selected || +ambiMonitor.value || 1;
  const focus = document.activeElement === ambiMonitor;
  ambiMonitor.innerHTML = items
    .map((d) => {
      const idx = +d.index;
      const sel = idx === +cur ? " selected" : "";
      return `<option value="${idx}"${sel}>${d.label || `Дисплей ${idx}`}</option>`;
    })
    .join("");
  if (![...ambiMonitor.options].some((o) => +o.value === +cur) && ambiMonitor.options.length) {
    ambiMonitor.value = ambiMonitor.options[0].value;
  } else {
    ambiMonitor.value = String(cur);
  }
  if (focus) ambiMonitor.focus();
}

briVal.textContent = briLabel(+bri.value);
spdVal.textContent = speedLabel(+spd.value);

window.__synclightSet = (state) => {
  if (!state) return;
  if (typeof state.brightness === "number") {
    bri.value = state.brightness;
    briVal.textContent = briLabel(state.brightness);
  }
  if (typeof state.speed === "number") {
    spd.value = state.speed;
    spdVal.textContent = speedLabel(state.speed);
  }
  if (typeof state.autostart === "boolean") autostart.checked = state.autostart;
  if (typeof state.mirror_room === "boolean") mirrorRoom.checked = state.mirror_room;
  if (state.displays || typeof state.ambi_monitor === "number") {
    renderDisplays(state.displays, state.ambi_monitor);
  }
  if (typeof state.status === "string") statusLine.textContent = state.status;
  if (typeof state.target === "string") {
    target = state.target;
    document.querySelectorAll(".zone").forEach((z) => z.classList.toggle("on", z.dataset.target === target));
  }
  if (typeof state.ambi === "boolean") {
    ambiOn = state.ambi;
    ambiBtn.classList.toggle("on", ambiOn);
  }
  if (state.bulb) renderBulb(state.bulb);
  if (state.bt) renderBt(state.bt);
  if (state.version_label) verBadge.textContent = state.version_label;
  else if (state.version && state.version.display) verBadge.textContent = state.version.display;
  if (state.ai_scenes) renderAi(state.ai_scenes, state.ai_scene || aiScene);
  else if (typeof state.ai_scene === "string") {
    aiScene = state.ai_scene;
    renderAi(null, aiScene);
  }
  if (aiMeta && state.ai_context) {
    const app = state.ai_context.app || "—";
    const live = state.ai_live ? " · следит" : "";
    aiMeta.textContent = `Сейчас: ${app}${live}`;
  }
};
