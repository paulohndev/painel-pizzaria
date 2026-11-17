# Liga o vigia do painel junto com o Windows (Agendador de Tarefas).
# Uso: agendar.ps1 [-Remover]
param([switch]$Remover)
$ErrorActionPreference = "Stop"
$nome = "Painel Mariá - vigia"
$antigo = "Painel Pizzaria - atualizar dados"

Unregister-ScheduledTask -TaskName $antigo -Confirm:$false -ErrorAction SilentlyContinue
if ($Remover) {
    Stop-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $nome -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Vigia desligado. O painel continua funcionando pelo 'Atualizar painel'."
    exit 0
}

$pasta = $PSScriptRoot
$script = Join-Path $pasta "vigia.py"

# O Agendador do Windows não consegue abrir os atalhos do Python da pasta WindowsApps.
# Por isso descobrimos o caminho real do Python instalado.
$real = $null
foreach ($cmd in @("py", "python", "python3")) {
    $c = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($c) {
        try {
            $args0 = @("-c", "import sys; print(sys.executable)")
            if ($cmd -eq "py") { $args0 = @("-3") + $args0 }
            $saida = & $c.Source @args0 2>$null
            if ($saida -and (Test-Path $saida.Trim())) { $real = $saida.Trim(); break }
        } catch {}
    }
}
if (-not $real) {
    Write-Host "Não encontrei o Python neste computador. Instale pelo python.org e rode de novo."
    exit 1
}
$pyw = Join-Path (Split-Path $real) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $real }

$acao = New-ScheduledTaskAction -Execute $pyw -Argument "`"$script`"" -WorkingDirectory $pasta
$gatilho = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$config = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
          -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $nome -Action $acao -Trigger $gatilho -Settings $config `
    -Description "Acompanha os pedidos do Cardápio Web, avisa oportunidades e serve o painel em http://127.0.0.1:8766 (pasta $pasta)" -Force | Out-Null
Start-ScheduledTask -TaskName $nome

Write-Host ""
Write-Host "Pronto! O vigia está ligado e vai ligar sozinho sempre que você entrar no Windows."
Write-Host "Python usado: $pyw"
Write-Host "Painel ao vivo: http://127.0.0.1:8766"
Write-Host "Registro do que ele faz: dados\vigia.log"
