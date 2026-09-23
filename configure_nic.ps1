# Check, and optionally set, receive-side scaling (RSS) on the camera NIC ports.
#
# WHY: a port that reports NumberOfReceiveQueues = 1 has RSS enabled but inert,
# so all of that port's packets are handled by one core's DPC. -Check judges
# each camera port's receive path; the apply path sets the RSS queue count and
# processor range and verifies what the driver kept. docs/HISTORY.md has the
# measurements: on the reference rig four queues changed nothing measurable, so
# the apply path is for a deliberate experiment, not a required setting.
#
# PREFLIGHT (-Check) reads and reports and writes nothing, so it is safe at any
# time, including during a recording. Run it from an ELEVATED PowerShell:
# without elevation Get-NetAdapterRss returns values that are not the
# adapter's settings (a queue count and processor range nobody configured,
# with MaxProcessors and RssProcessorArray blank), so an unelevated -Check
# reports the RSS check as not judged and says to re-run elevated. Either form
# works; the core numbers are an example, so pass the pool the GUI logs:
#   powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Check -CaptureCores 10,11,12,13
#   powershell -ExecutionPolicy Bypass -Command "& .\configure_nic.ps1 -Check -CaptureCores 10,11,12,13"
# A list parameter (-Ports, -CaptureCores) takes comma-separated values in both
# forms: through -File the list arrives as one string, which the script splits.
# An adapter whose name contains a comma therefore cannot be named with -Ports.
# It checks each camera port against four thresholds and prints PASS or WARN
# per check. Each threshold is receive-path margin, not preference:
#   * receive descriptors >= 2048 -- the ring is what absorbs a DPC that runs
#     late, so a short ring turns a scheduling hiccup into discarded packets.
#   * interrupt moderation off or at its lowest setting -- moderation trades
#     latency for interrupt rate, and a synchronised burst from several cameras
#     needs the ring drained promptly rather than efficiently.
#   * RSS enabled -- receive processing otherwise cannot leave one core.
#   * the NIC's DPCs off the capture cores -- a grab thread sharing a core with
#     its own NIC's DPC is the laggard, and the penalty follows the core. Pass
#     -CaptureCores with the pool the GUI logs ("[rig] capture core pool ..."),
#     which is the complement of the profile's capture_core_exclude; without it
#     the check reports where the DPCs are and judges nothing. A processor mask
#     counts only under DevicePolicy 4 (IrqPolicySpecifiedProcessors); under
#     any other policy Windows ignores it, and the check reports it as inactive.
#
# APPLYING (no -Check) RESETS every port it touches, so the cameras drop off the
# network and re-enumerate. Never run it during a recording. It needs
# elevation, and it refuses:
#   * while a Panopticon process (gui.py or a probe) is running, or when the
#     process table cannot be read; -Force overrides after checking by hand.
#   * ports it derived itself (Up adapters with a manual IPv4 address, a rule an
#     office adapter with a static address also matches), unless you name the
#     ports with -Ports or pass -Confirm to be asked before each one.
#   powershell -ExecutionPolicy Bypass -File configure_nic.ps1 -Ports "Ethernet 3,Ethernet 4"
# -WhatIf prints what would be applied and changes nothing.
#
# The defaults RESTORE the vendor RSS placement (1 queue, processors 0 to the
# last logical processor). Pass other values only for a deliberate,
# one-variable experiment, and re-run with the defaults to revert.
#
# VERIFICATION RULE: the settle poll and the final check compare against the
# value this run APPLIES. A check against a different number reports every run
# as failed (or every run as succeeded) whatever the driver did, which defeats
# the one thing this script promises.
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
# Do not confine RSS to a core subset (for example the E-cores with
# -BaseProcessor/-MaxProcessor) to keep DPC off the capture cores:
#   1. It does not move the DPC. RSS queue->processor mapping is not the same
#      knob as MSI-X interrupt affinity, which is what places a DPC; that
#      lives in the device's registry Interrupt Management\Affinity Policy key
#      (above) and needs a reboot.
#   2. It makes packet handling worse, because the NIC wants fast cores.
# It measured as a regression (docs/HISTORY.md, phase 6). In any experiment,
# change the queue count and the base processor one at a time.
# ---------------------------------------------------------------------------
# SupportsShouldProcess gives -WhatIf and -Confirm their standard meaning; the
# default impact never prompts on its own.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Empty means "derive it". Adapter names, the logical-processor count and
    # the core layout describe one machine, so none of them is a default here;
    # pass -Ports to override the derivation.
    [string[]] $Ports  = @(),
    # Defaults RESTORE the vendor RSS placement (1 queue, processors 0 to the
    # last logical processor; -MaxProcessor -1 means the last one); pass
    # other values only for a deliberate, one-variable experiment.
    [int]      $Queues        = 1,
    [int]      $BaseProcessor = 0,
    [int]      $MaxProcessor  = -1,
    # The capture core pool the GUI logs. Used by -Check only. Strings, not
    # [int[]]: see Split-List.
    [string[]] $CaptureCores  = @(),
    [int]      $MinReceiveBuffers = 2048,
    [switch]   $Check,
    # Apply even while a Panopticon process runs or the process table cannot
    # be read. For an operator who has checked the machine by hand.
    [switch]   $Force
)

$ErrorActionPreference = "Stop"

function Split-List($values) {
    # RULE: every list parameter is split on commas after binding. REASON:
    # "powershell -File" passes each argument as a string, so "-CaptureCores
    # 10,11,12" arrives as the single string "10,11,12", which an [int[]]
    # parameter refuses and a plain [string[]] one would treat as one adapter
    # name. Under -Command PowerShell builds the list itself, and splitting
    # its elements again changes nothing.
    $out = @()
    foreach ($v in $values) {
        foreach ($part in ([string]$v -split ',')) {
            $t = $part.Trim()
            if ($t) { $out += $t }
        }
    }
    # The unary comma keeps an empty result an empty array: PowerShell unrolls
    # a returned collection, and a bare `return $out` of @() emits $null.
    return ,$out
}

$Ports = Split-List $Ports
$CaptureCoreList = @()
foreach ($c in (Split-List $CaptureCores)) {
    $n = 0
    if (-not [int]::TryParse($c, [ref]$n) -or $n -lt 0) {
        Write-Host ("-CaptureCores: '{0}' is not a processor number" -f $c) -ForegroundColor Red
        exit 1
    }
    $CaptureCoreList += $n
}

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

$portsDerived = $false
if (-not $Ports -or $Ports.Count -eq 0) {
    $portsDerived = $true
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

function Get-DpcPolicy($port) {
    # The MSI-X affinity policy lives in the device's own registry key and in
    # no Get-NetAdapter* cmdlet, which is why an RSS setting cannot move a DPC.
    # Returns $null when the key cannot be read (it can need elevation), and
    # otherwise an object:
    #   Processors  the processors AssignmentSetOverride names (empty if unset)
    #   Policy      DevicePolicy, or $null when absent (the machine default, 0)
    #   Active      whether the mask places DPCs: only DevicePolicy 4
    #               (IrqPolicySpecifiedProcessors) with a non-empty mask does.
    # Returning an object, never a bare array, keeps "no policy set" and
    # "unreadable" apart: PowerShell unrolls an empty array on return, which
    # would otherwise arrive as $null, the unreadable answer.
    try {
        $id  = (Get-NetAdapter -Name $port -ErrorAction Stop).PnPDeviceID
        $key = "HKLM:\SYSTEM\CurrentControlSet\Enum\$id\Device Parameters\Interrupt Management\Affinity Policy"
        $procs = @()
        $policy = $null
        if (Test-Path $key) {
            $props = Get-ItemProperty -Path $key -ErrorAction Stop
            $mask = $props.AssignmentSetOverride
            $policy = $props.DevicePolicy
            if ($mask -is [byte[]]) {
                for ($b = 0; $b -lt $mask.Length; $b++) {
                    for ($bit = 0; $bit -lt 8; $bit++) {
                        if ($mask[$b] -band (1 -shl $bit)) { $procs += ($b * 8 + $bit) }
                    }
                }
            } elseif ($null -ne $mask) {
                $m = [uint64]$mask
                for ($i = 0; $i -lt 64; $i++) {
                    if ($m -band ([uint64]1 -shl $i)) { $procs += $i }
                }
            }
        }
        return [pscustomobject]@{
            Processors = @($procs)
            Policy     = $policy
            Active     = (($null -ne $policy) -and ([int]$policy -eq 4) -and ($procs.Count -gt 0))
        }
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

function Test-Elevated {
    return ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$isAdmin = Test-Elevated

function Invoke-Preflight {
    Write-Host ""
    Write-Host "=== NIC preflight (read-only; writes nothing) ===" -ForegroundColor Cyan
    Write-Host ("Thresholds: receive descriptors >= {0}, interrupt moderation off or" -f $MinReceiveBuffers)
    Write-Host "lowest, RSS enabled, and the NIC's DPCs off the capture cores."
    if (-not $isAdmin) {
        Write-Host ""
        Write-Host "Not elevated: the RSS values Windows reports without elevation are not" -ForegroundColor Yellow
        Write-Host "the adapter's settings, so the RSS check is not judged. Re-run from an" -ForegroundColor Yellow
        Write-Host "elevated PowerShell (right-click PowerShell -> Run as administrator)." -ForegroundColor Yellow
    }
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

        # RULE: no RSS verdict without elevation. REASON: unelevated,
        # Get-NetAdapterRss returns a queue count and processor range that are
        # not the adapter's settings, and printing them as PASS reports a
        # configuration the adapter does not have.
        if (-not $isAdmin) {
            Write-Verdict "RSS" $false "not judged: unelevated values are not the adapter's settings; re-run elevated"
        } else {
            try {
                $rss = Get-NetAdapterRss -Name $p -ErrorAction Stop
                Write-Verdict "RSS" ($rss.Enabled) ("enabled={0} queues={1} processors {2}-{3}" -f
                    $rss.Enabled, $rss.NumberOfReceiveQueues, $rss.BaseProcessorNumber, $rss.MaxProcessorNumber)
            } catch {
                Write-Verdict "RSS" $false ("no RSS information -- {0}" -f $_.Exception.Message)
            }
        }

        $dpc = Get-DpcPolicy $p
        if ($null -eq $dpc) {
            Write-Verdict "DPC affinity" $false "the affinity policy key is unreadable; re-run elevated"
        } else {
            $procs = @($dpc.Processors)
            if ($null -eq $dpc.Policy) { $policyText = "DevicePolicy unset" }
            else { $policyText = "DevicePolicy={0}" -f $dpc.Policy }
            if ($dpc.Active) {
                $where = "processors {0}" -f ($procs -join ",")
            } elseif ($procs.Count) {
                $where = ("a mask naming processors {0} is present but inactive ({1}; " +
                          "it applies only at 4), so DPCs land wherever Windows puts them") -f
                         ($procs -join ","), $policyText
            } else {
                $where = "unset, so DPCs land wherever Windows puts them"
            }
            if ($CaptureCoreList.Count -eq 0) {
                Write-Host ("    INFO  {0,-22} {1}; pass -CaptureCores to judge it" -f "DPC affinity", $where) -ForegroundColor DarkGray
            } elseif (-not $dpc.Active) {
                if ($procs.Count) { $detail = $where }
                else { $detail = "no policy set" }
                Write-Verdict "DPC affinity" $false ("{0}, so nothing keeps DPCs off the capture cores" -f $detail)
            } else {
                $clash = @($procs | Where-Object { $CaptureCoreList -contains $_ })
                Write-Verdict "DPC affinity" ($clash.Count -eq 0) ("processors {0}; capture cores {1}" -f
                    ($procs -join ","), ($CaptureCoreList -join ","))
            }
        }
    }
    Write-Host ""
    Write-Host "Read-only: nothing above was changed. Re-run without -Check to apply."
}

if ($Check) {
    Invoke-Preflight
    exit 0
}

if (-not $isAdmin) {
    Write-Host "This must run elevated (Set-NetAdapterRss needs admin)." -ForegroundColor Red
    Write-Host "Right-click PowerShell -> Run as administrator, then re-run." -ForegroundColor Red
    exit 1
}

function Get-PanopticonProcess {
    # Python processes running a Panopticon entry point: gui.py, or a probe_*.py
    # (the probes gui_app/probe_guard.py guards open cameras too). Returns
    # $null when the process table cannot be read.
    #
    # RULE: this process's own command line is the canary; a table in which it
    # is blank is unreadable, not empty. REASON: a guard that treats an
    # instrument failure as "nothing running" passes exactly when it cannot
    # see, which is the state it exists to catch.
    try {
        $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
        return $null
    }
    $self = $all | Where-Object { $_.ProcessId -eq $PID }
    if (-not $self -or -not $self.CommandLine) { return $null }
    $hits = @($all | Where-Object {
        $_.Name -like "python*.exe" -and $_.CommandLine -and
        $_.CommandLine -match '(?i)(^|[\\/\s"''])(gui|probe_\w+)\.py\b' })
    return ,$hits
}

function Get-ApplyRefusal($ports, $portsDerived, $confirmAsked, $force) {
    # Why the apply path must not run, as the lines to print, or $null when it
    # may. A function, so the rules can be exercised without applying.
    if (-not $force) {
        $running = Get-PanopticonProcess
        if ($null -eq $running) {
            return @("Cannot read the process table, so cannot tell whether Panopticon is",
                     "recording. Applying resets the camera adapters. Check by hand, then",
                     "re-run with -Force.")
        }
        if ($running.Count -gt 0) {
            $lines = @("Panopticon is running; applying would reset the camera adapters under it:")
            foreach ($r in $running) { $lines += ("  PID {0}: {1}" -f $r.ProcessId, $r.CommandLine) }
            $lines += "Close it first, or re-run with -Force if it is not recording."
            return $lines
        }
    }
    if ($portsDerived -and -not $confirmAsked) {
        return @(("Derived camera ports: {0}" -f ($ports -join ", ")),
                 "Applying resets every one of them. The derivation takes any Up adapter",
                 "with a manual IPv4 address, which an office adapter with a static address",
                 "matches too. Name the camera ports with -Ports, or pass -Confirm to be",
                 "asked before each one.")
    }
    return $null
}

# Nothing below changes an adapter under -WhatIf, so the guards that protect a
# running session and an unnamed adapter apply only to a real run.
if (-not $WhatIfPreference) {
    $confirmAsked = $PSBoundParameters.ContainsKey("Confirm") -and [bool]$PSBoundParameters["Confirm"]
    $refusal = Get-ApplyRefusal $Ports $portsDerived $confirmAsked $Force.IsPresent
    if ($null -ne $refusal) {
        foreach ($line in $refusal) { Write-Host $line -ForegroundColor Red }
        exit 1
    }
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

# Only the ports this run asked the driver to change are polled and verified:
# a port declined at a -Confirm prompt keeps its setting and is not a failure.
$applied = @()
foreach ($p in $Ports) {
    $what = "Set {0} receive queues on processors {1}-{2} (resets the adapter)" -f `
        $Queues, $BaseProcessor, $MaxProcessor
    if (-not $PSCmdlet.ShouldProcess($p, $what)) { continue }
    $applied += $p
    try {
        # -Confirm:$false: the prompt, when asked for, was the one above.
        Set-NetAdapterRss -Name $p -NumberOfReceiveQueues $Queues `
            -BaseProcessorNumber $BaseProcessor -MaxProcessorNumber $MaxProcessor `
            -Confirm:$false -ErrorAction Stop
        Write-Host ("  {0}: {1} queues on processors {2}-{3}" -f `
            $p, $Queues, $BaseProcessor, $MaxProcessor) -ForegroundColor Green
    } catch {
        Write-Host ("  {0}: FAILED -- {1}" -f $p, $_.Exception.Message) -ForegroundColor Red
    }
}
if ($WhatIfPreference) {
    Write-Host ""
    Write-Host "WhatIf: no adapter was changed."
    exit 0
}
if ($applied.Count -eq 0) {
    Write-Host ""
    Write-Host "No port was changed."
    exit 0
}
# From here on $Ports names only the ports this run asked the driver to
# change, so the poll, the read-back and the link-state table cover those.
$Ports = $applied

# The adapter reset is not instant, and a fixed wait is not good enough: a 5 s
# sleep read back the OLD value and reported "NOT APPLIED" for a change that had
# in fact taken effect moments later. Poll until it settles, and say how long it
# took rather than guessing.
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    # RULE: the poll counts the objects that came back before it believes them.
    # REASON: a Where-Object over an empty result matches nothing, so a read
    # that returned no adapter at all -- a port still down from the reset this
    # script itself causes -- otherwise reads as "every port already agrees"
    # and the poll prints "settled after 0s" without having seen one queue
    # count.
    $now = @(Get-NetAdapterRss -Name $Ports -ErrorAction SilentlyContinue)
    if ($now.Count -eq $Ports.Count -and
        -not ($now | Where-Object { $_.NumberOfReceiveQueues -ne $Queues })) {
        Write-Host ("  settled after {0:N0}s" -f `
            (60 - ($deadline - (Get-Date)).TotalSeconds)) -ForegroundColor DarkGray
        break
    }
    Start-Sleep -Seconds 2
}
Show-State "AFTER"

# Verify rather than assume: a driver that silently ignores the request is the
# expected failure mode here, not an error.
#
# RULE: read once, prove one object came back per port, and only then compare.
# REASON: iterating the query inline runs the loop body zero times when the
# query returns nothing, which leaves $bad empty and prints the green success
# line for ports that were never read -- a verification that passes hardest
# exactly when the instrument failed. That is the VERIFICATION RULE above,
# inverted.
$after  = @(Get-NetAdapterRss -Name $Ports -ErrorAction SilentlyContinue)
$silent = @($Ports | Where-Object { $port = $_
                                    -not ($after | Where-Object { $_.Name -eq $port }) })
$bad = @()
foreach ($r in $after) {
    if ($r.NumberOfReceiveQueues -ne $Queues) { $bad += $r.Name }
}
$verified = $true
Write-Host ""
if ($silent.Count -gt 0) {
    $verified = $false
    Write-Host ("NOT VERIFIED: no RSS information came back for {0}" -f `
        ($silent -join ", ")) -ForegroundColor Red
    Write-Host "The queue count may or may not have been applied: it could not be read back,"
    Write-Host "so this run proves nothing about those ports. A port still resetting"
    Write-Host "reappears within a minute; check the link state below and re-run elevated."
} elseif ($bad.Count -eq 0) {
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

# A run that could not read a port back exits nonzero: an operator script that
# chains on this one must not treat an unread port as a configured port.
if (-not $verified) { exit 1 }
