# Agenda a atualização diária do painel no Agendador de Tarefas do Windows.
# Uso: agendar.ps1 [-Hora 06:00] [-Remover]
param([string]$Hora = "06:00", [switch]$Remover)
$ErrorActionPreference = "Stop"
$nome = "Painel Pizzaria - atualizar dados"

if ($Remover) {
    Unregister-ScheduledTask -TaskName $nome -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Atualização diária desativada."
    exit 0
}

$pasta = $PSScriptRoot
$script = Join-Path $pasta "iniciar.py"
$exe = $null
$argumentos = $null
$pyw = Get-Command pyw -ErrorAction SilentlyContinue
if ($pyw) { $exe = $pyw.Source; $argumentos = "-3 `"$script`" --agendado" }
if (-not $exe) {
    $pw = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($pw) { $exe = $pw.Source; $argumentos = "`"$script`" --agendado" }
}
if (-not $exe) {
    Write-Host "Não encontrei o Python neste computador. Instale pelo python.org (marcando 'Add to PATH') e rode de novo."
    exit 1
}

$acao = New-ScheduledTaskAction -Execute $exe -Argument $argumentos -WorkingDirectory $pasta
$gatilho = New-ScheduledTaskTrigger -Daily -At $Hora
$config = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
          -ExecutionTimeLimit (New-TimeSpan -Hours 10) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $nome -Action $acao -Trigger $gatilho -Settings $config `
    -Description "Baixa pedidos do Cardápio Web e atualiza o painel.html (pasta $pasta)" -Force | Out-Null

Write-Host ""
Write-Host "Pronto! Todo dia às $Hora o painel será atualizado sozinho, sem abrir janela."
Write-Host "Se o computador estiver desligado nesse horário, ele atualiza assim que for ligado."
Write-Host "O registro de cada atualização fica em: dados\execucao.log"
