import QtQuick
import Quickshell
import Quickshell.Io
import qs.Common
import qs.Widgets
import qs.Modules.Plugins
import "usage.js" as Usage

PluginComponent {
    id: root

    readonly property int refreshMs: 60000
    // The first scan reads a month of transcripts; later ones only read appended bytes.
    readonly property int scanTimeoutMs: 120000
    readonly property bool fetchPrices: pluginData.fetchPrices ?? false
    readonly property bool ready: pluginService !== null && pluginId !== ""
    readonly property bool onlyWhenRunning: pluginData.onlyWhenRunning ?? true
    // Slow, because the widget only appears on activity anyway; activity triggers an extra check.
    readonly property int runningCheckMs: 20000
    readonly property bool notify: pluginData.notify ?? true
    readonly property int notifyPercent: 80
    // Two reports of one window can differ slightly in their reset time.
    readonly property int sameWindowMs: 120000
    readonly property var agentNames: ({
            claude: "Claude",
            codex: "Codex",
            opencode: "OpenCode"
        })
    property var notified: null
    readonly property real refreshRequest: refreshVar.value ?? 0

    onRefreshRequestChanged: {
        if (refreshRequest > 0)
            refresh();
    }

    PluginGlobalVar {
        id: refreshVar
        varName: "refreshRequest"
        defaultValue: 0
    }
    readonly property int idleMs: 300000
    readonly property var agentIds: ["claude", "codex", "opencode"]
    readonly property string cacheDir: (Quickshell.env("XDG_CACHE_HOME") || Quickshell.env("HOME") + "/.cache") + "/dms-agent-usage"

    function refresh() {
        if (!ready)
            return;
        const command = ["python3", pluginService.getPluginPath(pluginId) + "/collect.py"];
        if (fetchPrices)
            command.push("--fetch-prices");
        Proc.runCommand("agentUsage.collect", command, (stdout, exitCode) => {
            const snapshot = exitCode === 0 ? Usage.parseSnapshot(stdout) : null;
            if (!snapshot)
                return;
            pluginService.setGlobalVar(pluginId, "snapshot", snapshot);
            applyActivity(snapshot);
            applyNotifications(snapshot);
        }, 0, scanTimeoutMs, root);
    }

    onReadyChanged: {
        if (ready)
            refresh();
    }

    onFetchPricesChanged: refresh()

    // An agent counts as open when it runs in a terminal; their background helpers have no terminal.
    function checkRunning() {
        Proc.runCommand("agentUsage.running", ["ps", "-eo", "tty=,comm="], (stdout, exitCode) => {
            if (exitCode !== 0)
                return;
            const open = new Set();
            for (const line of stdout.split("\n")) {
                const parts = line.trim().split(/\s+/);
                if (parts.length === 2 && parts[0] !== "?" && agentIds.includes(parts[1]))
                    open.add(parts[1]);
            }
            applyRunning(agentIds.filter(id => open.has(id)));
        }, 0, undefined, root);
    }

    function applyRunning(running) {
        const previous = pluginService.globalVars[pluginId]?.running ?? null;
        if (previous && previous.join() === running.join())
            return;
        pluginService.setGlobalVar(pluginId, "running", running);
        // Follow the agent that was just opened; at startup, any open one if the saved choice is closed.
        const selected = pluginData.provider ?? "claude";
        const opened = running.filter(id => !(previous ?? []).includes(id) && agentShown(id));
        if (opened.length > 0 && (previous !== null || !running.includes(selected)))
            pluginService.savePluginData(pluginId, "provider", opened[0]);
    }

    function agentShown(id) {
        return pluginData["show" + id.charAt(0).toUpperCase() + id.slice(1)] ?? true;
    }

    Timer {
        interval: root.runningCheckMs
        repeat: true
        running: root.ready && root.onlyWhenRunning
        triggeredOnStart: true
        onTriggered: root.checkRunning()
    }

    // Swaps the new Claude limits into the last snapshot, so a reply shows up without running the collector.
    function applyLiveLimits(text) {
        const live = Usage.parseSnapshot(text);
        const limits = live?.limits;
        const snapshot = pluginService?.globalVars[pluginId]?.snapshot;
        if (!limits || !snapshot)
            return;
        // The file is also read at startup, when it can be older than what the collector just reported.
        if (limits.updatedMs <= (snapshot.claude?.limits?.updatedMs ?? 0))
            return;
        const merged = Object.assign({}, snapshot, {
            fetchedMs: Date.now(),
            claude: Object.assign({}, snapshot.claude, {
                limits: limits,
                activeMs: Math.max(snapshot.claude?.activeMs ?? 0, (live.active ?? 0) * 1000)
            })
        });
        pluginService.setGlobalVar(pluginId, "snapshot", merged);
        applyActivity(merged);
        applyNotifications(merged);
    }

    function windowName(entry) {
        if (entry.minutes === 10080)
            return I18n.trFor("agentUsage", "weekly");
        if (entry.minutes > 0 && entry.minutes < 1440)
            return I18n.trFor("agentUsage", "%1-hour").arg(Math.round(entry.minutes / 60));
        return I18n.trFor("agentUsage", "%1-day").arg(Math.round(entry.minutes / 1440));
    }

    // One notification per step per window: pace warning, then 80%, then the limit itself.
    function applyNotifications(snapshot) {
        if (!notify)
            return;
        if (notified === null)
            notified = pluginService.loadPluginState(pluginId, "notified", {}) ?? {};
        let changed = false;
        for (const id of agentIds.filter(agentShown)) {
            for (const entry of snapshot[id]?.limits?.windows ?? []) {
                const level = entry.tone === "full" ? 3 : entry.percent >= notifyPercent ? 2 : entry.tone === "warn" ? 1 : 0;
                const key = id + ":" + entry.id;
                const sameWindow = Math.abs((notified[key]?.resetsMs ?? 0) - entry.resetsMs) <= sameWindowMs;
                if (level <= (sameWindow ? (notified[key]?.level ?? 0) : 0))
                    continue;
                notified[key] = {
                    resetsMs: entry.resetsMs,
                    level: level
                };
                changed = true;
                sendNotification(agentNames[id], entry, level, snapshot.fetchedMs);
            }
        }
        if (changed)
            pluginService.savePluginState(pluginId, "notified", notified);
    }

    function sendNotification(agent, entry, level, now) {
        const name = windowName(entry);
        const resets = entry.resetsMs > 0 ? I18n.trFor("agentUsage", "Resets in %1.").arg(Usage.formatDuration(entry.resetsMs - now)) : "";
        let summary = I18n.trFor("agentUsage", "%1 %2 limit at %3%").arg(agent).arg(name).arg(entry.percent);
        let body = resets;
        if (level === 3)
            summary = I18n.trFor("agentUsage", "%1 %2 limit reached").arg(agent).arg(name);
        if (level === 1) {
            summary = I18n.trFor("agentUsage", "%1 %2 limit may run out early").arg(agent).arg(name);
            body = I18n.trFor("agentUsage", "At this pace it runs out in %1.").arg(Usage.formatDuration(entry.runsOutMs - now)) + " " + resets;
        }
        Quickshell.execDetached(["dms", "notify", "-a", "Dux Agent Usage", summary, body]);
    }

    // Idle means no shown agent has done anything for a while. Coming back from idle selects the agent that woke it.
    function applyActivity(snapshot) {
        const shown = agentIds.filter(agentShown);
        const latest = shown.reduce((best, id) => (snapshot[id]?.activeMs ?? 0) > (snapshot[best]?.activeMs ?? 0) ? id : best, shown[0]);
        const idle = snapshot.fetchedMs - (snapshot[latest]?.activeMs ?? 0) > idleMs;
        const wasIdle = pluginService.globalVars[pluginId]?.idle ?? null;
        if (idle === wasIdle)
            return;
        pluginService.setGlobalVar(pluginId, "idle", idle);
        if (!idle && onlyWhenRunning)
            checkRunning();
        if (wasIdle && !idle && latest !== (pluginData.provider ?? "claude"))
            pluginService.savePluginData(pluginId, "provider", latest);
    }

    // statusline.py rewrites this after every Claude Code reply, which makes the widget live.
    FileView {
        id: liveFile

        path: root.cacheDir + "/claude-limits.json"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: root.applyLiveLimits(liveFile.text())
    }

    Timer {
        interval: root.refreshMs
        repeat: true
        running: root.ready
        onTriggered: root.refresh()
    }
}
