import QtQuick
import qs.Common
import qs.Widgets
import qs.DCommon.Widgets
import qs.Modules.Plugins
import "usage.js" as Usage

PluginComponent {
    id: root

    readonly property var agents: [
        {
            id: "claude",
            name: "Claude",
            shown: pluginData.showClaude ?? true
        },
        {
            id: "codex",
            name: "Codex",
            shown: pluginData.showCodex ?? true
        },
        {
            id: "opencode",
            name: "OpenCode",
            shown: pluginData.showOpencode ?? true
        }
    ].filter(agent => agent.shown)
    readonly property var providerNames: agents.map(agent => agent.name)
    readonly property int providerIndex: Math.max(0, agents.findIndex(agent => agent.id === (pluginData.provider ?? "claude")))
    readonly property string providerId: agents[providerIndex]?.id ?? ""
    readonly property string providerName: agents[providerIndex]?.name ?? ""

    readonly property bool onlyWhenRunning: pluginData.onlyWhenRunning ?? true
    readonly property var running: runningVar.value ?? []
    readonly property bool hideWhenIdle: pluginData.hideWhenIdle ?? true
    readonly property bool idle: idleVar.value ?? true
    readonly property bool pillShown: agents.length > 0 && (!onlyWhenRunning || agents.some(agent => running.includes(agent.id))) && !(hideWhenIdle && idle)

    onPillShownChanged: setVisibilityOverride(pillShown)
    Component.onCompleted: setVisibilityOverride(pillShown)
    readonly property bool showLabels: pluginData.showLabels ?? true
    readonly property string claudeWindow: pluginData.claudeWindow ?? "five_hour"

    readonly property url providerIcon: Qt.resolvedUrl("icons/" + providerId + ".svg")
    // Claude's colour is in its file. The other marks are monochrome: white on dark themes, black on light ones.
    readonly property color logoTint: providerId === "claude" ? "transparent" : (Theme.isLightMode ? "black" : "white")
    // A logo fills its whole box, where a Material glyph of the same size has padding around it.
    readonly property int logoSize: Math.round(iconSize * 0.8)

    readonly property var snapshot: snapshotVar.value
    readonly property var provider: snapshot?.[providerId] ?? null
    readonly property var limits: provider?.limits ?? null
    readonly property var windows: limits?.windows ?? []
    readonly property var usage: provider?.usage ?? null
    readonly property var daily: usage?.daily ?? []
    readonly property real now: snapshot?.fetchedMs ?? 0
    // OpenCode reports no limits, so its pill shows today's use instead of a percentage.
    readonly property bool hasLimits: providerId !== "opencode"
    readonly property var headline: providerId === "claude" ? Usage.findWindow(limits, claudeWindow) : Usage.busiestWindow(limits)
    readonly property string todayText: !usage ? "--" : (usage.todayCost > 0 ? Usage.formatCost(usage.todayCost) : Usage.formatTokens(usage.todayTokens))
    readonly property string pillText: !hasLimits ? todayText : (headline ? headline.percent + "%" : "--")
    readonly property color pillColor: toneColor(headline?.tone, Theme.widgetTextColor)
    readonly property real peakCost: daily.reduce((peak, day) => Math.max(peak, day[1]), 0)
    readonly property real peakTokens: daily.reduce((peak, day) => Math.max(peak, day[2]), 0)

    function toneColor(tone, fallback) {
        switch (tone) {
        case "full":
            return Theme.error;
        case "warn":
            return Theme.warning;
        default:
            return fallback;
        }
    }

    function windowLabel(entry) {
        const suffix = entry.model ? " (" + entry.model + ")" : "";
        if (entry.minutes <= 0)
            return I18n.trFor("agentUsage", "Limit") + suffix;
        if (entry.minutes < 1440)
            return I18n.trFor("agentUsage", "%1-hour limit").arg(Math.round(entry.minutes / 60)) + suffix;
        if (entry.minutes === 10080)
            return I18n.trFor("agentUsage", "Weekly limit") + suffix;
        return I18n.trFor("agentUsage", "%1-day limit").arg(Math.round(entry.minutes / 1440)) + suffix;
    }

    function windowNote(entry) {
        if (entry.tone === "full" && entry.resetsMs > 0)
            return I18n.trFor("agentUsage", "Limit reached, resets in %1").arg(Usage.formatDuration(entry.resetsMs - now));
        if (entry.tone === "warn")
            return I18n.trFor("agentUsage", "Runs out in %1, resets in %2").arg(Usage.formatDuration(entry.runsOutMs - now)).arg(Usage.formatDuration(entry.resetsMs - now));
        if (entry.resetsMs > 0)
            return I18n.trFor("agentUsage", "Resets in %1").arg(Usage.formatDuration(entry.resetsMs - now));
        return "";
    }

    function limitsNote() {
        if (!snapshot)
            return I18n.trFor("agentUsage", "Reading usage…");
        if (!limits)
            return providerId === "codex" ? I18n.trFor("agentUsage", "No limit data yet. Run Codex once.") : I18n.trFor("agentUsage", "No limit data yet. Send a message in Claude Code.");
        if (limits.stale)
            return I18n.trFor("agentUsage", "Limits last read %1 ago.").arg(Usage.formatDuration(now - limits.updatedMs));
        return "";
    }

    // Days with no price still show their tokens, so an unpriced tool has a chart.
    function dayShare(index) {
        const day = daily[index];
        if (!day)
            return 0;
        if (peakCost > 0)
            return day[1] / peakCost;
        return peakTokens > 0 ? day[2] / peakTokens : 0;
    }

    pillRightClickAction: () => pluginService?.setGlobalVar(pluginId, "refreshRequest", Date.now())

    PluginGlobalVar {
        id: snapshotVar
        varName: "snapshot"
        defaultValue: null
    }

    PluginGlobalVar {
        id: runningVar
        varName: "running"
        defaultValue: null
    }

    PluginGlobalVar {
        id: idleVar
        varName: "idle"
        defaultValue: null
    }

    horizontalBarPill: Component {
        Item {
            implicitWidth: pillRow.implicitWidth
            implicitHeight: pillRow.implicitHeight
            clip: true

            Behavior on implicitWidth {
                enabled: !SettingsData.reduceMotion
                NumberAnimation {
                    duration: Theme.shortDuration
                    easing.type: Theme.standardEasing
                }
            }

            Row {
                id: pillRow

                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                spacing: Theme.spacingXS

                DSVGIcon {
                    source: root.providerIcon
                    size: root.logoSize
                    colorOverride: root.logoTint
                    anchors.verticalCenter: parent.verticalCenter
                }

                StyledText {
                    visible: root.showLabels
                    text: root.providerName
                    font.pixelSize: root.textSize
                    color: Theme.widgetTextColor
                    anchors.verticalCenter: parent.verticalCenter
                }

                StyledText {
                    text: root.pillText
                    font.pixelSize: root.textSize
                    font.weight: Theme.fontWeightMedium
                    color: root.pillColor
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
        }
    }

    verticalBarPill: Component {
        Column {
            spacing: 1

            DSVGIcon {
                source: root.providerIcon
                size: root.logoSize
                colorOverride: root.logoTint
                anchors.horizontalCenter: parent.horizontalCenter
            }

            StyledText {
                text: root.hasLimits && root.headline ? root.headline.percent : root.pillText
                font.pixelSize: root.textSize
                font.weight: Theme.fontWeightMedium
                color: root.pillColor
                anchors.horizontalCenter: parent.horizontalCenter
            }
        }
    }

    popoutWidth: 360

    popoutContent: Component {
        PopoutComponent {
            headerText: root.limits?.plan ? root.providerName + " · " + root.limits.plan : root.providerName
            detailsText: I18n.trFor("agentUsage", "Read from local files")

            Column {
                width: parent.width
                spacing: Theme.spacingM

                DButtonGroup {
                    visible: root.agents.length > 1
                    anchors.horizontalCenter: parent.horizontalCenter
                    arrowKeysSelect: false
                    model: root.providerNames
                    currentIndex: root.providerIndex
                    onSelectionChanged: (index, selected) => {
                        if (selected && index !== root.providerIndex)
                            root.pluginService?.savePluginData(root.pluginId, "provider", root.agents[index].id);
                    }
                }

                StyledRect {
                    visible: root.hasLimits
                    width: parent.width
                    height: limitsColumn.implicitHeight + Theme.spacingM * 2
                    radius: Theme.cornerRadius
                    color: Theme.surfaceContainerHigh

                    Column {
                        id: limitsColumn

                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: Theme.spacingM
                        spacing: Theme.spacingS

                        StyledText {
                            text: I18n.trFor("agentUsage", "Usage limits")
                            font.pixelSize: Theme.fontSizeMedium
                            font.weight: Theme.fontWeightMedium
                            color: Theme.surfaceText
                        }

                        Repeater {
                            // A count keeps the rows alive across refreshes, so only their values change.
                            model: root.windows.length

                            Column {
                                id: windowRow

                                required property int index
                                readonly property var entry: root.windows[index] ?? null

                                visible: entry !== null
                                width: parent.width
                                spacing: Theme.spacingXS

                                Item {
                                    width: parent.width
                                    height: windowName.implicitHeight

                                    StyledText {
                                        id: windowName
                                        text: windowRow.entry ? root.windowLabel(windowRow.entry) : ""
                                        font.pixelSize: Theme.fontSizeSmall
                                        color: Theme.surfaceText
                                    }

                                    StyledText {
                                        anchors.right: parent.right
                                        text: (windowRow.entry?.percent ?? 0) + "%"
                                        font.pixelSize: Theme.fontSizeSmall
                                        font.weight: Theme.fontWeightMedium
                                        color: root.toneColor(windowRow.entry?.tone, Theme.surfaceText)
                                    }
                                }

                                StyledRect {
                                    width: parent.width
                                    height: Theme.spacingXS
                                    radius: Theme.cornerRadiusFull
                                    color: Theme.outlineVariant

                                    StyledRect {
                                        width: parent.width * (windowRow.entry?.percent ?? 0) / 100
                                        height: parent.height
                                        radius: parent.radius
                                        color: root.toneColor(windowRow.entry?.tone, Theme.primary)

                                        Behavior on width {
                                            enabled: !SettingsData.reduceMotion
                                            NumberAnimation {
                                                duration: Theme.shortDuration
                                                easing.type: Theme.standardEasing
                                            }
                                        }
                                    }
                                }

                                StyledText {
                                    visible: text !== ""
                                    text: windowRow.entry ? root.windowNote(windowRow.entry) : ""
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: root.toneColor(windowRow.entry?.tone, Theme.surfaceVariantText)
                                }
                            }
                        }

                        StyledText {
                            visible: text !== ""
                            width: parent.width
                            wrapMode: Text.WordWrap
                            text: root.limitsNote()
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.surfaceVariantText
                        }
                    }
                }

                StyledRect {
                    width: parent.width
                    height: usageColumn.implicitHeight + Theme.spacingM * 2
                    radius: Theme.cornerRadius
                    color: Theme.surfaceContainerHigh

                    Column {
                        id: usageColumn

                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: Theme.spacingM
                        spacing: Theme.spacingS

                        StyledText {
                            text: I18n.trFor("agentUsage", "Last 30 days")
                            font.pixelSize: Theme.fontSizeMedium
                            font.weight: Theme.fontWeightMedium
                            color: Theme.surfaceText
                        }

                        Row {
                            width: parent.width

                            Figure {
                                value: Usage.formatTokens(root.usage?.tokens ?? 0)
                                label: I18n.trFor("agentUsage", "Tokens")
                            }

                            Figure {
                                value: Usage.formatCost(root.usage?.cost ?? 0)
                                label: I18n.trFor("agentUsage", "API-equivalent cost")
                            }

                            Figure {
                                value: root.todayText
                                label: I18n.trFor("agentUsage", "Today")
                            }
                        }

                        Row {
                            id: chart

                            readonly property real barHeight: Theme.iconSizeLarge * 2

                            width: parent.width
                            height: barHeight
                            spacing: Theme.spacingXS / 2

                            Repeater {
                                model: 30

                                StyledRect {
                                    required property int index
                                    readonly property real share: root.dayShare(index)

                                    anchors.bottom: parent.bottom
                                    width: (chart.width - chart.spacing * 29) / 30
                                    height: Math.max(Theme.spacingXS / 2, chart.barHeight * share)
                                    radius: Theme.cornerRadiusSmall
                                    color: share > 0 ? Theme.primary : Theme.outlineVariant

                                    Behavior on height {
                                        enabled: !SettingsData.reduceMotion
                                        NumberAnimation {
                                            duration: Theme.shortDuration
                                            easing.type: Theme.standardEasing
                                        }
                                    }
                                }
                            }
                        }

                        StyledText {
                            visible: (root.usage?.unpriced.length ?? 0) > 0
                            width: parent.width
                            wrapMode: Text.WordWrap
                            text: I18n.trFor("agentUsage", "No price for %1, so its cost is not counted.").arg((root.usage?.unpriced ?? []).join(", "))
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.surfaceVariantText
                        }
                    }
                }
            }
        }
    }

    component Figure: Column {
        property string value: ""
        property string label: ""

        width: parent.width / 3

        StyledText {
            text: parent.value
            font.pixelSize: Theme.fontSizeLarge
            font.weight: Theme.fontWeightMedium
            color: Theme.surfaceText
        }

        StyledText {
            text: parent.label
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceVariantText
        }
    }
}
