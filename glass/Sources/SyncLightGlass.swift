import SwiftUI
import AppKit

let projectRoot = "/Users/admin/Projects/synclight"
let pythonPath = "/usr/bin/python3"

@main
struct SyncLightApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup("SyncLight") {
            ContentView()
                .frame(minWidth: 420, minHeight: 640)
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 440, height: 720)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

enum Bridge {
    @discardableResult
    static func run(_ args: [String]) throws -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonPath)
        proc.arguments = args
        proc.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        let out = Pipe()
        let err = Pipe()
        proc.standardOutput = out
        proc.standardError = err
        try proc.run()
        proc.waitUntilExit()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        let errData = err.fileHandleForReading.readDataToEndOfFile()
        if proc.terminationStatus != 0 {
            let msg = String(data: errData, encoding: .utf8) ?? "exit \(proc.terminationStatus)"
            throw NSError(domain: "SyncLight", code: Int(proc.terminationStatus), userInfo: [NSLocalizedDescriptionKey: msg])
        }
        return String(data: data, encoding: .utf8) ?? ""
    }

    static func sl(_ args: String...) throws -> String {
        try run([projectRoot + "/sl.py"] + args)
    }
}

struct EffectItem: Identifiable, Hashable {
    let id: Int
    let name: String
}

let dynamicEffects: [EffectItem] = [
    .init(id: 0, name: "Rainbow Flow"),
    .init(id: 1, name: "Breathing"),
    .init(id: 2, name: "Color Chase"),
    .init(id: 3, name: "Meteor"),
    .init(id: 4, name: "Sparkle"),
    .init(id: 5, name: "Gradient"),
    .init(id: 6, name: "Marquee"),
    .init(id: 7, name: "Twist"),
    .init(id: 8, name: "Beat"),
    .init(id: 9, name: "Twirl"),
    .init(id: 10, name: "Lemon"),
    .init(id: 11, name: "Electric"),
]

let soundEffects: [EffectItem] = [
    .init(id: 0, name: "Rhythm Wave"),
    .init(id: 1, name: "Rhythm Pulse"),
    .init(id: 2, name: "Rhythm Spectrum"),
    .init(id: 3, name: "Rhythm Flash"),
    .init(id: 4, name: "Rhythm Gradient"),
    .init(id: 5, name: "Rhythm Chase"),
    .init(id: 6, name: "Rhythm Rainbow"),
]

struct ContentView: View {
    @State private var brightness: Double = 200
    @State private var speed: Double = 50
    @State private var ambiOn = false
    @State private var autostart = false
    @State private var status = "Ready"
    @State private var busy = false
    @State private var selectedTab = 0
    @State private var ambiProcess: Process?
    @State private var glowHue: Double = 0.55

    var body: some View {
        ZStack {
            AtmosphereBackground(hue: glowHue)
            VStack(spacing: 18) {
                header
                GlassCard {
                    VStack(alignment: .leading, spacing: 14) {
                        Text(status)
                            .font(.system(size: 13, weight: .medium, design: .rounded))
                            .foregroundStyle(.secondary)
                        HStack(spacing: 10) {
                            GlassButton(title: ambiOn ? "Stop Ambi" : "Ambilight", symbol: ambiOn ? "stop.fill" : "sparkles", accent: true) {
                                toggleAmbi()
                            }
                            GlassButton(title: "On", symbol: "lightbulb.fill") { solidOn() }
                            GlassButton(title: "Off", symbol: "lightbulb") { runAsync { try Bridge.sl("off"); setStatus("Off") } }
                        }
                    }
                }

                GlassCard {
                    VStack(alignment: .leading, spacing: 16) {
                        labeledSlider(title: "Brightness", value: $brightness, range: 5...255, symbol: "sun.max.fill") {
                            runAsync {
                                try Bridge.sl("brightness", "\(Int(brightness))")
                                setStatus("Brightness \(Int(brightness))")
                            }
                        }
                        labeledSlider(title: "Speed", value: $speed, range: 5...100, symbol: "gauge.with.dots.needle.67percent") {
                            runAsync {
                                try Bridge.sl("speed", "\(Int(speed))")
                                setStatus("Speed \(Int(speed))")
                            }
                        }
                    }
                }

                Picker("", selection: $selectedTab) {
                    Text("Effects").tag(0)
                    Text("Music").tag(1)
                    Text("Colors").tag(2)
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 2)

                ScrollView {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 118), spacing: 10)], spacing: 10) {
                        if selectedTab == 0 {
                            ForEach(dynamicEffects) { item in
                                EffectChip(title: item.name) {
                                    glowHue = Double(item.id) / 12.0
                                    runAsync {
                                        try Bridge.sl("effect", "\(item.id)")
                                        try Bridge.sl("brightness", "\(Int(brightness))")
                                        try Bridge.sl("speed", "\(Int(speed))")
                                        setStatus(item.name)
                                    }
                                }
                            }
                        } else if selectedTab == 1 {
                            ForEach(soundEffects) { item in
                                EffectChip(title: item.name) {
                                    runAsync {
                                        try Bridge.sl("sound", "\(item.id)")
                                        try Bridge.sl("brightness", "\(Int(brightness))")
                                        setStatus(item.name)
                                    }
                                }
                            }
                        } else {
                            ForEach(["warm", "cool", "white", "red", "green", "blue", "purple"], id: \.self) { name in
                                EffectChip(title: name.capitalized) {
                                    runAsync {
                                        try Bridge.sl("color", name)
                                        try Bridge.sl("brightness", "\(Int(brightness))")
                                        setStatus(name.capitalized)
                                    }
                                }
                            }
                        }
                    }
                }
                .frame(maxHeight: 260)

                GlassCard {
                    Toggle(isOn: $autostart) {
                        Label("Open at Login", systemImage: "power.circle.fill")
                            .font(.system(size: 14, weight: .semibold, design: .rounded))
                    }
                    .toggleStyle(.switch)
                    .onChange(of: autostart) { _, newValue in
                        setAutostart(newValue)
                    }
                }
            }
            .padding(22)
        }
        .onAppear {
            autostart = FileManager.default.fileExists(
                atPath: NSHomeDirectory() + "/Library/LaunchAgents/com.robobloq.synclight.gui.plist"
            )
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            ZStack {
                Circle()
                    .fill(.ultraThinMaterial)
                    .frame(width: 44, height: 44)
                Image(systemName: "light.max")
                    .font(.system(size: 18, weight: .semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(.primary)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text("SyncLight")
                    .font(.system(size: 28, weight: .bold, design: .rounded))
                Text("Liquid control")
                    .font(.system(size: 13, weight: .medium, design: .rounded))
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
    }

    private func labeledSlider(title: String, value: Binding<Double>, range: ClosedRange<Double>, symbol: String, onCommit: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(title, systemImage: symbol)
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                Spacer()
                Text("\(Int(value.wrappedValue))")
                    .font(.system(size: 13, weight: .medium, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Slider(value: value, in: range, onEditingChanged: { editing in
                if !editing { onCommit() }
            })
        }
    }

    private func setStatus(_ text: String) {
        DispatchQueue.main.async { status = text }
    }

    private func runAsync(_ work: @escaping () throws -> Void) {
        guard !busy else { return }
        busy = true
        DispatchQueue.global(qos: .userInitiated).async {
            defer { DispatchQueue.main.async { busy = false } }
            do {
                try work()
            } catch {
                setStatus(error.localizedDescription)
            }
        }
    }

    private func solidOn() {
        runAsync {
            try Bridge.sl("on")
            try Bridge.sl("brightness", "\(Int(brightness))")
            setStatus("On")
        }
    }

    private func toggleAmbi() {
        if ambiOn {
            ambiProcess?.terminate()
            ambiProcess = nil
            ambiOn = false
            setStatus("Ambilight off")
            return
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonPath)
        proc.arguments = [projectRoot + "/ambilight.py"]
        proc.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
            ambiProcess = proc
            ambiOn = true
            setStatus("Ambilight on")
        } catch {
            setStatus(error.localizedDescription)
        }
    }

    private func setAutostart(_ enabled: Bool) {
        runAsync {
            let script = projectRoot + "/dialog_gui.py"
            // reuse python helper via tiny inline
            let code = """
import sys
sys.path.insert(0, \(projectRoot.debugDescription))
from dialog_gui import set_autostart
set_autostart(\(enabled ? "True" : "False"))
"""
            try _ = Bridge.run(["-c", code])
            setStatus(enabled ? "Autostart on" : "Autostart off")
        }
    }
}

struct AtmosphereBackground: View {
    var hue: Double
    var body: some View {
        ZStack {
            LinearGradient(
                colors: [
                    Color(hue: hue, saturation: 0.35, brightness: 0.22),
                    Color(hue: (hue + 0.12).truncatingRemainder(dividingBy: 1), saturation: 0.25, brightness: 0.12),
                    Color(red: 0.05, green: 0.06, blue: 0.08),
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            Circle()
                .fill(Color(hue: hue, saturation: 0.55, brightness: 0.85).opacity(0.35))
                .frame(width: 280, height: 280)
                .blur(radius: 60)
                .offset(x: -120, y: -180)
            Circle()
                .fill(Color(hue: (hue + 0.4).truncatingRemainder(dividingBy: 1), saturation: 0.4, brightness: 0.9).opacity(0.22))
                .frame(width: 260, height: 260)
                .blur(radius: 70)
                .offset(x: 140, y: 220)
        }
        .ignoresSafeArea()
        .animation(.easeInOut(duration: 0.8), value: hue)
    }
}

struct GlassCard<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        content
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background {
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .fill(.ultraThinMaterial)
                    .overlay {
                        RoundedRectangle(cornerRadius: 22, style: .continuous)
                            .strokeBorder(
                                LinearGradient(
                                    colors: [
                                        .white.opacity(0.55),
                                        .white.opacity(0.08),
                                        .white.opacity(0.25),
                                    ],
                                    startPoint: .topLeading,
                                    endPoint: .bottomTrailing
                                ),
                                lineWidth: 1
                            )
                    }
                    .shadow(color: .black.opacity(0.25), radius: 24, y: 10)
            }
    }
}

struct GlassButton: View {
    let title: String
    var symbol: String? = nil
    var accent = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                if let symbol {
                    Image(systemName: symbol)
                }
                Text(title)
                    .lineLimit(1)
            }
            .font(.system(size: 13, weight: .semibold, design: .rounded))
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity)
            .background {
                Capsule(style: .continuous)
                    .fill(accent ? AnyShapeStyle(Color.accentColor.opacity(0.85)) : AnyShapeStyle(.thinMaterial))
                    .overlay {
                        Capsule(style: .continuous)
                            .strokeBorder(.white.opacity(0.35), lineWidth: 1)
                    }
            }
            .foregroundStyle(accent ? .white : .primary)
        }
        .buttonStyle(.plain)
    }
}

struct EffectChip: View {
    let title: String
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity, minHeight: 52)
                .padding(.horizontal, 8)
                .background {
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(.thinMaterial)
                        .overlay {
                            RoundedRectangle(cornerRadius: 16, style: .continuous)
                                .strokeBorder(
                                    LinearGradient(
                                        colors: [.white.opacity(0.5), .white.opacity(0.1)],
                                        startPoint: .top,
                                        endPoint: .bottom
                                    ),
                                    lineWidth: 1
                                )
                        }
                }
        }
        .buttonStyle(.plain)
    }
}
