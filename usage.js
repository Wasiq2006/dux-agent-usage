.pragma library

function parseSnapshot(text) {
    try {
        return JSON.parse(text);
    } catch (e) {
        return null;
    }
}

function findWindow(limits, id) {
    return limits?.windows.find(entry => entry.id === id) ?? null;
}

// The window closest to its limit is the one worth showing when space is tight.
function busiestWindow(limits) {
    if (!limits || limits.windows.length === 0)
        return null;
    return limits.windows.reduce((worst, entry) => entry.percent > worst.percent ? entry : worst, limits.windows[0]);
}

function formatDuration(ms) {
    const minutes = Math.max(0, Math.floor(ms / 60000));
    const days = Math.floor(minutes / 1440);
    const hours = Math.floor((minutes % 1440) / 60);
    if (days > 0)
        return days + "d " + hours + "h";
    if (hours > 0)
        return hours + "h " + (minutes % 60) + "m";
    return minutes + "m";
}

function formatTokens(count) {
    if (count >= 1e9)
        return (count / 1e9).toFixed(2) + "B";
    if (count >= 1e6)
        return (count / 1e6).toFixed(1) + "M";
    if (count >= 1e3)
        return Math.round(count / 1e3) + "k";
    return String(count);
}

function formatCost(usd) {
    return "$" + (usd >= 100 ? Math.round(usd) : usd.toFixed(2));
}
