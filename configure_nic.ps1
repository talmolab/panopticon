# Spread GigE receive processing across cores on the camera NIC ports.
#
# WHY (measured 2026-09-03, docs/PERF_EXPERIMENTS.md E5):
#   Both camera ports report NumberOfReceiveQueues = 1, so RSS is enabled but
#   inert: each port's ~78,000 packets/s funnel through a single core's DPC.
#   During a 150 s 6-camera recording, cores 0 and 1 sat at 45.7% and 45.4%
#   DPC time while the 24-core average was 3.96%. Ethernet 5 discarded 35,423
#   packets at the NIC over that run; Ethernet 4 discarded zero. UDPv4 receive
#   errors were 0 (so it is not socket-buffer overflow) and packet errors were 0
#   (so it is not corruption on the wire) -- what is left is the receive ring
#   being serviced too slowly, i.e. host-side scheduling.
#
#   Frame loss today is already near zero because pylon's resends recover those
#   discards. The point of this change is MARGIN FOR 9 CAMERAS: a third port
#   adds a third DPC-bound core, and 46% is not where you want to begin a 50%
#   increase in packet rate.
#
# WHY IT SHOULD WORK: Windows hashes non-TCP IPv4 on the source/destination
#   2-tuple, and the three cameras on each port have distinct IPs, so they
#   should land on different queues. Some Intel drivers ignore the setting for
#   non-TCP traffic, which is why this script VERIFIES rather than assumes.
#
# REVERTING: re-run with -Queues 1.
#
# Applying this RESETS both adapters, so the cameras briefly disappear and
# re-enumerate. Never run it during a recording.
#
# Run ELEVATED:
#   powershell -ExecutionPolicy Bypass -File configure_nic.ps1
# ---------------------------------------------------------------------------
# 2026-09-11: STEER NIC DPC OFF THE P-CORES. This is now the main point of this
# script; the queue count above changed nothing measurable.
#
# The rig CPU is hybrid: 8 P-cores at logicals 0,1,10,11,12,13,22,23 and 16
# E-cores at 2-9 and 14-21. RSS defaults to BaseProcessorNumber 0, so receive
# processing lands on logicals 0,1,2 -- and TWO OF THOSE ARE P-CORES.
#
# Measured during a NINE-camera recording (2026-09-11, the first time DPC has
# been sampled at nine; E5's numbers were from six):
#     core 0  55.2% DPC   <- P-core
#     core 1  56.3% DPC   <- P-core
#     core 2  66.4% DPC   <- E-core, the third port as E5 predicted
#     everything else     <3%
#
# !!! TESTED 2026-09-11 AND REVERTED. DO NOT RE-APPLY WITHOUT READING THIS. !!!
# Setting -BaseProcessorNumber 2 -MaxProcessorNumber 9 (confining RSS to
# E-cores) was a REGRESSION on two counts:
#   1. It did not move the DPC at all. Cores 0/1 stayed at 57.6% each and
#      core 2 at 68.4%. RSS queue->processor mapping is not the same knob as
#      MSI-X interrupt affinity, which is what actually places the DPC; that
#      lives in the device's registry Interrupt Management\Affinity Policy key
#      and needs a reboot. E6 reached the same conclusion from queue counts.
#   2. It made packet handling WORSE, because the NIC wants fast cores:
#        Eth5 resends 12,060 -> 35,080, median lag 1.7 -> 3.3, max 6.0 -> 15.3
#        worst camera overall 5/9/11 -> 7/18/25
#      Ethernet 3 improved slightly (24,848 -> 16,603) but nowhere near enough
#      to pay for Ethernet 5.
# The DPC sitting on two P-cores is therefore not waste to be reclaimed -- it
# is the receive path needing the fast cores. Reverted with:
#   Set-NetAdapterRss -Name <ports> -NumberOfReceiveQueues 1 `
#       -BaseProcessorNumber 0 -MaxProcessorNumber 23
# CAVEAT on the measurement: the script changed queue count AND base processor
# in the same run (1->4 and 0->2), so the regression is not attributed to the
# base processor alone. If anyone revisits this, move ONE at a time.
# At six cameras cores 0/1 were ~46%. So a quarter of the P-core budget is
# consumed by interrupts before a single frame is grabbed, and the grab threads
# pinned to those cores are measurably the laggards: moving a camera off core 0
# took it from 5/9/12 to 1/7/9 frames behind, and the penalty followed the core
# to whichever camera replaced it.
#
# -BaseProcessorNumber 2 -MaxProcessorNumber 9 confines RSS to E-cores 2-9,
# freeing both P-cores. Reversible with -BaseProcessorNumber 0. Resets the
# adapters, so never run it with a recording in flight.
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [string[]] $Ports  = @("Ethernet 3", "Ethernet 4", "Ethernet 5"),
    [int]      $Queues = 4,
    # Defaults are the RESTORE values, not the experiment: see the block above.
    [int]      $Queues2       = 1,
    [int]      $BaseProcessor = 0,
    [int]      $MaxProcessor  = 23
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This must run elevated (Set-NetAdapterRss needs admin)." -ForegroundColor Red
    Write-Host "Right-click PowerShell -> Run as administrator, then re-run." -ForegroundColor Red
    exit 1
}

function Show-State($label) {
    Write-Host ""
    Write-Host "=== $label ===" -ForegroundColor Cyan
    Get-NetAdapterRss -Name $Ports |
        Select-Object Name, Enabled, NumberOfReceiveQueues,
                      BaseProcessorNumber, MaxProcessorNumber |
        Format-Table -AutoSize
    Write-Host "  (P-cores on this part are 0,1,10,11,12,13,22,23 - RSS should avoid them)" -ForegroundColor DarkGray
}

Show-State "BEFORE"

foreach ($p in $Ports) {
    try {
        Set-NetAdapterRss -Name $p -NumberOfReceiveQueues $Queues2 `
            -BaseProcessorNumber $BaseProcessor -MaxProcessorNumber $MaxProcessor `
            -ErrorAction Stop
        Write-Host ("  {0}: {1} queues on processors {2}-{3}" -f `
            $p, $Queues, $BaseProcessor, $MaxProcessor) -ForegroundColor Green
    } catch {
        Write-Host ("  {0}: FAILED -- {1}" -f $p, $_.Exception.Message) -ForegroundColor Red
    }
}

# The adapter reset is not instant, and a fixed wait is not good enough: a 5 s
# sleep read back the OLD value and reported "NOT APPLIED" for a change that had
# in fact taken effect moments later. Poll until it settles, and say how long it
# took rather than guessing.
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    $now = Get-NetAdapterRss -Name $Ports
    if (-not ($now | Where-Object { $_.NumberOfReceiveQueues -lt $Queues })) {
        Write-Host ("  settled after {0:N0}s" -f `
            (60 - ($deadline - (Get-Date)).TotalSeconds)) -ForegroundColor DarkGray
        break
    }
    Start-Sleep -Seconds 2
}
Show-State "AFTER"

# Verify rather than assume: a driver that silently ignores the request is the
# expected failure mode here, not an error.
$bad = @()
foreach ($r in (Get-NetAdapterRss -Name $Ports)) {
    if ($r.NumberOfReceiveQueues -lt $Queues) { $bad += $r.Name }
}
Write-Host ""
if ($bad.Count -eq 0) {
    Write-Host "OK: every port reports $Queues receive queues." -ForegroundColor Green
    Write-Host "Next: re-run the acquisition and compare Eth5 ReceivedDiscardedPackets"
    Write-Host "(baseline 35,423 per 150 s) and % DPC Time on cores 0/1 (baseline ~46%)."
} else {
    Write-Host ("NOT APPLIED on: {0}" -f ($bad -join ", ")) -ForegroundColor Yellow
    Write-Host "The driver accepted the call but kept fewer queues -- this happens when"
    Write-Host "a driver only applies RSS to TCP. Fallback is to tune *RssBaseProcNumber"
    Write-Host "and *MaxRssProcessors via Set-NetAdapterAdvancedProperty instead."
}

Write-Host ""
Write-Host "Camera link state (should be Up on both):" -ForegroundColor Cyan
Get-NetAdapter -Name $Ports | Select-Object Name, Status, LinkSpeed | Format-Table -AutoSize
