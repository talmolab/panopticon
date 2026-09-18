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
# REVERTING: re-run with -Queues 1, the default.
#
# VERIFICATION RULE: the settle poll and the final check compare against the
# value this run APPLIES. A check against a different number reports every run
# as failed (or every run as succeeded) whatever the driver did, which defeats
# the one thing this script promises.
#
# Applying this RESETS both adapters, so the cameras briefly disappear and
# re-enumerate. Never run it during a recording.
#
# Run ELEVATED:
#   powershell -ExecutionPolicy Bypass -File configure_nic.ps1
#
# PREFLIGHT (-Check) reads and reports and writes nothing, so it is safe at any
# time, including during a recording:
#   powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Check `
#       -CaptureCores 10,11,12,13,22,23
# It checks each camera port against four thresholds and prints PASS or WARN
# per check. Each threshold is receive-path margin, not preference:
#   * receive descriptors >= 2048 -- the ring is what absorbs a DPC that runs
#     late, so a short ring turns a scheduling hiccup into discarded packets.
#   * interrupt moderation off or at its lowest setting -- moderation trades
#     latency for interrupt rate, and a synchronised burst from three cameras
#     needs the ring drained promptly rather than efficiently.
#   * RSS enabled -- receive processing otherwise cannot leave one core.
#   * the NIC's DPCs off the capture cores -- a grab thread sharing a core with
#     its own NIC's DPC is the laggard, and the penalty follows the core. Pass
#     -CaptureCores with the pool the GUI logs ("[rig] capture core pool ..."),
#     which is the complement of the profile's capture_core_exclude; without it
#     the check reports where the DPCs are and judges nothing.
#
# MOVING THE DPCs IS REVERSIBLE, AND THIS SCRIPT DOES NOT DO IT. The knob is
# IrqPolicySpecifiedProcessors plus AssignmentSetOverride under the device's
# Interrupt Management\Affinity Policy key, it needs a reboot, and it is the
# only knob that places a DPC -- RSS queue-to-processor mapping is a different
# thing and does not move one. Export the key before touching it, and re-import
# to undo:
#   reg export "HKLM\SYSTEM\CurrentControlSet\Enum\<PnPDeviceID>\Device Parameters\Interrupt Management\Affinity Policy" affinity_backup.reg
#   reg import affinity_backup.reg
# Measure it with the per-core % DPC Time method before adopting it: a setting
# that does not move those counters has changed nothing.
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
    # Empty means "derive it". Adapter names, the logical-processor count and
    # the core layout describe one machine, so none of them is a default here;
    # pass -Ports to override the derivation.
    [string[]] $Ports  = @(),
    # Defaults are the RESTORE values, not the experiment: see the block above.
    [int]      $Queues        = 1,
    [int]      $BaseProcessor = 0,
    [int]      $MaxProcessor  = -1,
    # The capture core pool the GUI logs. Used by -Check only.
    [int[]]    $CaptureCores  = @(),
    [int]      $MinReceiveBuffers = 2048,
    [switch]   $Check
)

$ErrorActionPreference = "Stop"

function Get-CameraPort {
    # A camera port is an Up adapter holding a MANUALLY assigned IPv4 address:
    # each camera subnet gets a static host address, while every DHCP or
    # link-local adapter belongs to something else. Deriving the list beats a
    # hardcoded one, which throws Get-NetAdapterRss on any other host.
    $manual = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.PrefixOrigin -eq "Manual" -and
                       $_.IPAddress -notlike "127.*" } |
        Select-Object -ExpandProperty InterfaceAlias -Unique
    Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq "Up" -and $manual -contains $_.Name } |
        Select-Object -ExpandProperty Name
}

if (-not $Ports -or $Ports.Count -eq 0) {
    $Ports = @(Get-CameraPort)
    if ($Ports.Count -eq 0) {
        Write-Host "No camera port found: no Up adapter carries a manually" -ForegroundColor Red
        Write-Host "assigned IPv4 address. Pass -Ports explicitly." -ForegroundColor Red
        exit 1
    }
    Write-Host ("Derived camera ports: {0}" -f ($Ports -join ", ")) -ForegroundColor DarkGray
}
if ($MaxProcessor -lt 0) {
    $MaxProcessor = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors - 1
}

function Get-AdvancedValue($port, $keywords) {
    # Vendors spell the same knob differently, so try each spelling and return
    # the first that exists rather than assuming one driver's naming.
    foreach ($kw in $keywords) {
        try {
            $prop = Get-NetAdapterAdvancedProperty -Name $port -RegistryKeyword $kw -ErrorAction Stop
            if ($prop) {
                return [pscustomobject]@{
                    Keyword = $kw
                    Value   = $prop.RegistryValue[0]
                    Display = $prop.DisplayValue
                }
            }
        } catch { }
    }
    return $null
}

function Get-DpcProcessor($port) {
    # The MSI-X affinity policy lives in the device's own registry key and in
    # no Get-NetAdapter* cmdlet, which is why an RSS setting cannot move a DPC.
    # Returns the processors the policy names, @() when no policy is set, or
    # $null when the key cannot be read, which needs elevation.
    try {
        $id  = (Get-NetAdapter -Name $port -ErrorAction Stop).PnPDeviceID
        $key = "HKLM:\SYSTEM\CurrentControlSet\Enum\$id\Device Parameters\Interrupt Management\Affinity Policy"
        if (-not (Test-Path $key)) { return @() }
        $mask = (Get-ItemProperty -Path $key -ErrorAction Stop).AssignmentSetOverride
        if ($null -eq $mask) { return @() }
        $procs = @()
        if ($mask -is [byte[]]) {
            for ($b = 0; $b -lt $mask.Length; $b++) {
                for ($bit = 0; $bit -lt 8; $bit++) {
                    if ($mask[$b] -band (1 -shl $bit)) { $procs += ($b * 8 + $bit) }
                }
            }
        } else {
            $m = [uint64]$mask
            for ($i = 0; $i -lt 64; $i++) {
                if ($m -band ([uint64]1 -shl $i)) { $procs += $i }
            }
        }
        return $procs
    } catch {
        return $null
    }
}

function Write-Verdict($label, $ok, $detail) {
    if ($ok) {
        Write-Host ("    PASS  {0,-22} {1}" -f $label, $detail) -ForegroundColor Green
    } else {
        Write-Host ("    WARN  {0,-22} {1}" -f $label, $detail) -ForegroundColor Yellow
    }
}

function Invoke-Preflight {
    Write-Host ""
    Write-Host "=== NIC preflight (read-only; writes nothing) ===" -ForegroundColor Cyan
    Write-Host ("Thresholds: receive descriptors >= {0}, interrupt moderation off or" -f $MinReceiveBuffers)
    Write-Host "lowest, RSS enabled, and the NIC's DPCs off the capture cores."
    foreach ($p in $Ports) {
        Write-Host ""
        Write-Host ("  {0}" -f $p) -ForegroundColor Cyan

        $buf = Get-AdvancedValue $p @("*ReceiveBuffers", "ReceiveBuffers", "*ReceiveDescriptors")
        if ($null -eq $buf) {
            Write-Verdict "receive descriptors" $false "the driver exposes no receive-buffer keyword"
        } else {
            $n = [int]$buf.Value
            Write-Verdict "receive descriptors" ($n -ge $MinReceiveBuffers) ("{0} (want >= {1})" -f $n, $MinReceiveBuffers)
        }

        $mod = Get-AdvancedValue $p @("*InterruptModeration", "InterruptModeration")
        $itr = Get-AdvancedValue $p @("ITR", "*InterruptModerationRate")
        if (($null -eq $mod) -and ($null -eq $itr)) {
            Write-Verdict "interrupt moderation" $false "the driver exposes no moderation keyword"
        } else {
            $modOff = ($null -ne $mod) -and ([int]$mod.Value -eq 0)
            $itrLow = ($null -ne $itr) -and ([int]$itr.Value -eq 0)
            $shown  = @()
            if ($null -ne $mod) { $shown += ("moderation={0}" -f $mod.Display) }
            if ($null -ne $itr) { $shown += ("rate={0}" -f $itr.Display) }
            Write-Verdict "interrupt moderation" ($modOff -or $itrLow) ($shown -join " ")
        }

        try {
            $rss = Get-NetAdapterRss -Name $p -ErrorAction Stop
            Write-Verdict "RSS" ($rss.Enabled) ("enabled={0} queues={1} processors {2}-{3}" -f
                $rss.Enabled, $rss.NumberOfReceiveQueues, $rss.BaseProcessorNumber, $rss.MaxProcessorNumber)
        } catch {
            Write-Verdict "RSS" $false ("no RSS information -- {0}" -f $_.Exception.Message)
        }

        $dpc = Get-DpcProcessor $p
        if ($null -eq $dpc) {
            Write-Verdict "DPC affinity" $false "the affinity policy key is unreadable; re-run elevated"
        } elseif ($CaptureCores.Count -eq 0) {
            if ($dpc.Count) { $where = $dpc -join "," }
            else { $where = "unset, so DPCs land wherever Windows puts them" }
            Write-Host ("    INFO  {0,-22} {1}; pass -CaptureCores to judge it" -f "DPC affinity", $where) -ForegroundColor DarkGray
        } elseif ($dpc.Count -eq 0) {
            Write-Verdict "DPC affinity" $false "no policy set, so nothing keeps DPCs off the capture cores"
        } else {
            $clash = @($dpc | Where-Object { $CaptureCores -contains $_ })
            Write-Verdict "DPC affinity" ($clash.Count -eq 0) ("processors {0}; capture cores {1}" -f
                ($dpc -join ","), ($CaptureCores -join ","))
        }
    }
    Write-Host ""
    Write-Host "Read-only: nothing above was changed. Re-run without -Check to apply."
}

if ($Check) {
    Invoke-Preflight
    exit 0
}

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
    # Per port and inside a try: a name that does not resolve reports itself
    # instead of aborting the run under $ErrorActionPreference = "Stop".
    $rows = @()
    foreach ($p in $Ports) {
        try {
            $rows += Get-NetAdapterRss -Name $p -ErrorAction Stop |
                Select-Object Name, Enabled, NumberOfReceiveQueues,
                              BaseProcessorNumber, MaxProcessorNumber
        } catch {
            Write-Host ("  {0}: no RSS information -- {1}" -f
                $p, $_.Exception.Message) -ForegroundColor Yellow
        }
    }
    if ($rows) { $rows | Format-Table -AutoSize }
    Write-Host "  (RSS placement is not DPC placement - use -Check for the DPC test)" -ForegroundColor DarkGray
}

Show-State "BEFORE"

foreach ($p in $Ports) {
    try {
        Set-NetAdapterRss -Name $p -NumberOfReceiveQueues $Queues `
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
    $now = Get-NetAdapterRss -Name $Ports -ErrorAction SilentlyContinue
    if (-not ($now | Where-Object { $_.NumberOfReceiveQueues -ne $Queues })) {
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
foreach ($r in (Get-NetAdapterRss -Name $Ports -ErrorAction SilentlyContinue)) {
    if ($r.NumberOfReceiveQueues -ne $Queues) { $bad += $r.Name }
}
Write-Host ""
if ($bad.Count -eq 0) {
    Write-Host "OK: every port reports $Queues receive queues." -ForegroundColor Green
    Write-Host "Next: run a recording and compare each port's ReceivedDiscardedPackets"
    Write-Host "and per-core % DPC Time against the same numbers taken before this run."
    Write-Host "A setting that does not move those counters has changed nothing."
} else {
    Write-Host ("NOT APPLIED on: {0}" -f ($bad -join ", ")) -ForegroundColor Yellow
    Write-Host "The driver accepted the call but kept a different queue count -- this happens when"
    Write-Host "a driver only applies RSS to TCP. Fallback is to tune *RssBaseProcNumber"
    Write-Host "and *MaxRssProcessors via Set-NetAdapterAdvancedProperty instead."
}

Write-Host ""
Write-Host "Camera link state (every port must be Up):" -ForegroundColor Cyan
Get-NetAdapter -Name $Ports -ErrorAction SilentlyContinue |
    Select-Object Name, Status, LinkSpeed | Format-Table -AutoSize
