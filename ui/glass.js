const EFFECTS = [
  [0, "Rainbow Flow", "Smooth spectrum"],
  [1, "Breathing", "Soft pulse"],
  [2, "Color Chase", "Running colors"],
  [3, "Meteor", "Comet trail"],
  [4, "Sparkle", "Twinkle points"],
  [5, "Gradient", "Soft blend"],
  [6, "Marquee", "Theatre chase"],
];
const MUSIC = [
  [0, "Rhythm Wave", "Mic reactive"],
  [1, "Rhythm Pulse", "Mic reactive"],
  [2, "Rhythm Spectrum", "Mic reactive"],
  [3, "Rhythm Flash", "Mic reactive"],
  [4, "Rhythm Gradient", "Mic reactive"],
  [5, "Rhythm Chase", "Mic reactive"],
  [6, "Rhythm Rainbow", "Mic reactive"],
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

const modeLine = document.getElementById("modeLine");
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

let ambiOn = false;
let selected = { kind: null, id: null };

function setMode(text, cls) {
  modeLine.textContent = text;
  preview.classList.remove("mode-static", "mode-music", "mode-ambi", "mode-off", "mode-effect");
  if (cls) preview.classList.add(cls);
}

function bridge(payload) {
  if (window.webkit?.messageHandlers?.synclight) {
    window.webkit.messageHandlers.synclight.postMessage(payload);
  } else {
    console.log("bridge", payload);
  }
}

function speedLabel(v) {
  if (v < 25) return "Slow";
  if (v < 45) return "Easy";
  if (v < 65) return "Normal";
  if (v < 85) return "Fast";
  return "Max";
}

function briLabel(v) {
  return `${Math.round((v / 255) * 100)}%`;
}

function clearSelected() {
  document.querySelectorAll(".chip.selected").forEach((el) => el.classList.remove("selected"));
}

function fillEffects(id, items, kind) {
  const el = document.getElementById(id);
  el.innerHTML = "";
  items.forEach(([idx, name, desc]) => {
    const btn = document.createElement("button");
    btn.className = "chip";
    btn.innerHTML = `<span>${name}</span><small>${desc}</small>`;
    btn.onclick = () => {
      clearSelected();
      btn.classList.add("selected");
      selected = { kind, id: idx };
      ambiOn = false;
      ambiBtn.classList.remove("on");
      ambiBtn.textContent = "Ambilight";
      sensRow.hidden = kind !== "sound";
      bridge({
        action: kind,
        index: idx,
        brightness: +bri.value,
        speed: +spd.value,
        sensitivity: +sens.value,
      });
      setMode(`${name}`, kind === "sound" ? "mode-music" : "mode-effect");
    };
    el.appendChild(btn);
  });
}

function fillColors() {
  const el = document.getElementById("panel-colors");
  el.innerHTML = "";
  Object.entries(COLORS).forEach(([name, hex]) => {
    const btn = document.createElement("button");
    btn.className = "chip";
    btn.innerHTML = `<span class="swatch" style="background:${hex}"></span><span>${name[0].toUpperCase()}${name.slice(1)}</span><small>Solid color</small>`;
    btn.onclick = () => {
      clearSelected();
      btn.classList.add("selected");
      selected = { kind: "color", id: name };
      ambiOn = false;
      ambiBtn.classList.remove("on");
      ambiBtn.textContent = "Ambilight";
      sensRow.hidden = true;
      bridge({ action: "color", name, brightness: +bri.value });
      setMode(`${name} · Solid`, "mode-static");
      preview.querySelector(".strip").style.background = hex;
    };
    el.appendChild(btn);
  });
}

fillEffects("panel-effects", EFFECTS, "effect");
fillEffects("panel-music", MUSIC, "sound");
fillColors();

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    const name = tab.dataset.tab;
    document.querySelectorAll(".tab-panel").forEach((p) => {
      const on = p.id === `panel-${name}`;
      p.hidden = !on;
      p.classList.toggle("active", on);
    });
    sensRow.hidden = name !== "music" || selected.kind !== "sound";
  });
});

document.querySelectorAll("[data-cmd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const cmd = btn.dataset.cmd;
    if (cmd === "ambi") {
      ambiOn = !ambiOn;
      btn.classList.toggle("on", ambiOn);
      btn.textContent = ambiOn ? "Stop Ambi" : "Ambilight";
      clearSelected();
      sensRow.hidden = true;
      bridge({ action: "ambi", enabled: ambiOn });
      setMode(ambiOn ? "Ambilight" : "Ready", ambiOn ? "mode-ambi" : "mode-static");
      return;
    }
    clearSelected();
    ambiOn = false;
    ambiBtn.classList.remove("on");
    ambiBtn.textContent = "Ambilight";
    sensRow.hidden = true;
    bridge({ action: cmd, brightness: +bri.value });
    if (cmd === "on") setMode("Solid On", "mode-static");
    if (cmd === "off") setMode("Off", "mode-off");
  });
});

bri.addEventListener("input", () => { briVal.textContent = briLabel(+bri.value); });
spd.addEventListener("input", () => { spdVal.textContent = speedLabel(+spd.value); });
sens.addEventListener("input", () => { sensVal.textContent = sens.value; });
bri.addEventListener("change", () => bridge({ action: "brightness", value: +bri.value }));
spd.addEventListener("change", () => bridge({ action: "speed", value: +spd.value }));
sens.addEventListener("change", () => bridge({ action: "sensitivity", value: +sens.value }));

autostart.addEventListener("change", () => {
  bridge({ action: "autostart", enabled: autostart.checked });
});

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
  if (typeof state.status === "string") modeLine.textContent = state.status;
};
