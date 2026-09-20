$ErrorActionPreference = 'Stop'
$probeId = [guid]::NewGuid().ToString('N').Substring(0, 8)
$workspacePath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$roots = @{
    temp = Join-Path ([IO.Path]::GetTempPath()) ('satellitesense-socket-' + $probeId)
    local_app_data = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('satellitesense-socket-' + $probeId)
    workspace = Join-Path $workspacePath ('output\diagnostics\socket-' + $probeId)
}
$results = @()
foreach ($location in $roots.Keys) {
    $probeDirectory = [IO.Path]::GetFullPath($roots[$location])
    New-Item -ItemType Directory -Path $probeDirectory | Out-Null
    $path = Join-Path $probeDirectory 'probe.sock'
    $row = [ordered]@{location=$location; path=$path; bind=$false; close=$false; exists_after_close=$false; delete=$false}
    $socket = $null
    try {
        $socket = [Net.Sockets.Socket]::new([Net.Sockets.AddressFamily]::Unix, [Net.Sockets.SocketType]::Stream, [Net.Sockets.ProtocolType]::Unspecified)
        $socket.Bind([Net.Sockets.UnixDomainSocketEndPoint]::new($path))
        $socket.Listen(1)
        $row.bind = $true
        $row.attributes_while_open = [string](Get-Item -LiteralPath $path -Force).Attributes
    } catch {
        $row.bind_error = $_.Exception.Message
    } finally {
        if ($null -ne $socket) { $socket.Dispose(); $row.close=$true }
    }
    $row.exists_after_close = Test-Path -LiteralPath $path
    if ($row.exists_after_close) {
        try { Remove-Item -LiteralPath $path -Force; $row.delete=$true }
        catch { $row.delete_error=$_.Exception.Message }
    } else { $row.delete=$true }
    $row.remaining = Test-Path -LiteralPath $path
    if (-not $row.remaining) { Remove-Item -LiteralPath $probeDirectory }
    $results += [pscustomobject]$row
}
$report = Join-Path $workspacePath 'output\docker-socket-diagnostic.json'
$results | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $report -Encoding utf8
$results | ConvertTo-Json -Depth 4
