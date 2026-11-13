import QtQuick
import qs.Common
import qs.Services
import qs.Widgets
import qs.DCommon.Widgets
import qs.Modules.Plugins

PluginSettings {
    id: root

    pluginId: "agentUsage"

    // installed, missing, other (another status line is configured) or empty while checking.
    property string liveStatus: ""

    function liveSetup(action) {
        const script = PluginService.getPluginPath(pluginId) + "/statusline.py";
        Proc.runCommand("agentUsage.liveSetup", ["python3", script, action], (stdout, exitCode) => {
            if (exitCode === 0)
                root.liveStatus = stdout.trim();
        }, 0, undefined, root);
    }

    Component.onCompleted: liveSetup("--status")

    Column {
        width: parent.width
        spacing: Theme.spacingS

        StyledText {
            text: I18n.trFor("agentUsage", "Live Claude limits")
            font.pixelSize: Theme.fontSizeMedium
            font.weight: Theme.fontWeightMedium
            color: Theme.surfaceText
        }

        StyledText {
            width: parent.width
            wrapMode: Text.WordWrap
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.surfaceVariantText
            text: {
                switch (root.liveStatus) {
                case "installed":
                    return I18n.trFor("agentUsage", "On. Claude Code reports its limits to this widget after every reply.");
                case "other":
                    return I18n.trFor("agentUsage", "Claude Code already has a custom status line, so this was left alone. Call statusline.py from your own script to get live limits.");
                case "missing":
                    return I18n.trFor("agentUsage", "Off. Turning this on adds a status line entry to Claude Code's settings.json.");
                default:
                    return "";
                }
            }
        }

        DButton {
            visible: root.liveStatus === "installed" || root.liveStatus === "missing"
            text: root.liveStatus === "installed" ? I18n.trFor("agentUsage", "Turn off") : I18n.trFor("agentUsage", "Turn on")
            onClicked: root.liveSetup(root.liveStatus === "installed" ? "--uninstall" : "--install")
        }
    }

    ToggleSetting {
        settingKey: "showClaude"
        label: I18n.trFor("agentUsage", "Claude Code")
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "showCodex"
        label: I18n.trFor("agentUsage", "Codex")
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "showOpencode"
        label: I18n.trFor("agentUsage", "OpenCode")
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "onlyWhenRunning"
        label: I18n.trFor("agentUsage", "Show only while an agent is open")
        description: I18n.trFor("agentUsage", "Hides the widget until one of the agents above is running in a terminal, and switches to the one that was opened")
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "hideWhenIdle"
        label: I18n.trFor("agentUsage", "Hide when idle")
        description: I18n.trFor("agentUsage", "Hides the widget after 5 minutes without agent activity, until you send a new prompt")
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "notify"
        label: I18n.trFor("agentUsage", "Limit notifications")
        description: I18n.trFor("agentUsage", "Notifies once when a limit is on pace to run out early, reaches 80%, and is used up")
        defaultValue: true
    }

    ToggleSetting {
        settingKey: "showLabels"
        label: I18n.trFor("agentUsage", "Show names in the bar")
        description: I18n.trFor("agentUsage", "Turn off to show only the icon and percentage")
        defaultValue: true
    }

    SelectionSetting {
        settingKey: "claudeWindow"
        label: I18n.trFor("agentUsage", "Claude limit shown in the bar")
        options: [
            {
                label: I18n.trFor("agentUsage", "5-hour limit"),
                value: "five_hour"
            },
            {
                label: I18n.trFor("agentUsage", "Weekly limit"),
                value: "seven_day"
            }
        ]
        defaultValue: "five_hour"
    }

    ToggleSetting {
        settingKey: "fetchPrices"
        label: I18n.trFor("agentUsage", "Download model prices")
        description: I18n.trFor("agentUsage", "Gets the public LiteLLM price list at most once a day, to price models that are not Claude models. Nothing about your usage is sent.")
        defaultValue: false
    }
}
