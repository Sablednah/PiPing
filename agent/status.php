<?php
/**
 * status.php - per-host monitoring agent for the Pi status panel.
 *
 * Reports server stats (load, memory, disk) using NON-ROOT methods only,
 * so it works on shared hosting. Protected by a shared secret token.
 *
 * DEPLOY:
 *   1. Copy this file somewhere web-accessible on the host, e.g.
 *      /home/you/public_html/_status/status.php
 *      (a non-obvious path is good; the token is the real protection)
 *   2. Set the token below (or via env var STATUS_TOKEN).
 *   3. Test:  curl -H "X-Status-Token: YOURTOKEN" https://thesite/_status/status.php
 *
 * The Pi sends header:  X-Status-Token: <token>
 * Anything without the correct token gets a 403.
 */

// ---------------------------------------------------------------------------
// CONFIG
// ---------------------------------------------------------------------------
// Prefer an environment variable if your host lets you set one; otherwise
// edit the fallback string. The script REFUSES TO RUN if left as the default.
$EXPECTED_TOKEN = getenv('STATUS_TOKEN') ?: 'thereisnosp00m';

// Optional: the path whose disk usage you want to report. Default = this
// script's own filesystem, which on shared hosting is your account's volume.
$DISK_PATH = __DIR__;

// ---------------------------------------------------------------------------
// SAFETY: never run with the placeholder token
// ---------------------------------------------------------------------------
if ($EXPECTED_TOKEN === 'CHANGE-ME-to-a-long-random-string') {
    http_response_code(500);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'agent not configured: set STATUS_TOKEN']);
    exit;
}

// ---------------------------------------------------------------------------
// AUTH
// ---------------------------------------------------------------------------
$provided = $_SERVER['HTTP_X_STATUS_TOKEN'] ?? '';
if (!is_string($provided) || !hash_equals($EXPECTED_TOKEN, $provided)) {
    http_response_code(403);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'forbidden']);
    exit;
}

// ---------------------------------------------------------------------------
// STAT COLLECTION (all non-root, all feature-detected)
// ---------------------------------------------------------------------------

/** CPU load average, normalised against core count. Returns null if unreadable. */
function get_load(): ?array {
    if (!@is_readable('/proc/loadavg')) return null;
    $raw = @file_get_contents('/proc/loadavg');
    if ($raw === false) return null;
    $parts = preg_split('/\s+/', trim($raw));
    if (count($parts) < 3) return null;

    $cores = 1;
    if (@is_readable('/proc/cpuinfo')) {
        $cpu = @file_get_contents('/proc/cpuinfo');
        if ($cpu !== false) {
            $n = substr_count($cpu, "\nprocessor");
            // substr_count misses the first "processor" if at start; add 1 if present
            if (strpos($cpu, 'processor') === 0) $n++;
            if ($n > 0) $cores = $n;
        }
    }
    $load1 = (float)$parts[0];
    return [
        'load1'   => $load1,
        'load5'   => (float)$parts[1],
        'load15'  => (float)$parts[2],
        'cores'   => $cores,
        // percent = how saturated the box is on the 1-min figure (capped at 100)
        'percent' => min(100, round(($load1 / $cores) * 100)),
    ];
}

/** Memory from /proc/meminfo. On shared hosting this is the WHOLE box, flagged. */
function get_mem(): ?array {
    if (!@is_readable('/proc/meminfo')) return null;
    $raw = @file_get_contents('/proc/meminfo');
    if ($raw === false) return null;
    $vals = [];
    foreach (explode("\n", $raw) as $line) {
        if (preg_match('/^(\w+):\s+(\d+)\s*kB/', $line, $m)) {
            $vals[$m[1]] = (int)$m[2]; // kB
        }
    }
    if (!isset($vals['MemTotal'])) return null;
    $total = $vals['MemTotal'];
    // MemAvailable is the honest "free-ish" figure on modern kernels
    $avail = $vals['MemAvailable'] ?? ($vals['MemFree'] ?? 0);
    $used  = $total - $avail;
    return [
        'total_mb' => round($total / 1024),
        'used_mb'  => round($used / 1024),
        'percent'  => $total > 0 ? round(($used / $total) * 100) : null,
        'note'     => 'whole-host figure on shared hosting',
    ];
}

/** Disk usage of the account's filesystem via PHP built-ins (no shell). */
function get_disk(string $path): ?array {
    $free  = @disk_free_space($path);
    $total = @disk_total_space($path);
    if ($free === false || $total === false || $total <= 0) return null;
    $used = $total - $free;
    return [
        'total_gb' => round($total / 1073741824, 1),
        'used_gb'  => round($used  / 1073741824, 1),
        'percent'  => round(($used / $total) * 100),
    ];
}

// ---------------------------------------------------------------------------
// OUTPUT
// ---------------------------------------------------------------------------
$response = [
    'ok'        => true,
    'hostname'  => php_uname('n'),
    'time'      => time(),
    'php'       => PHP_VERSION,
    'cpu'       => get_load(),
    'memory'    => get_mem(),
    'disk'      => get_disk($DISK_PATH),
];

header('Content-Type: application/json');
header('Cache-Control: no-store');
echo json_encode($response, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES);
