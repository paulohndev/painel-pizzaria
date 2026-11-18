# Liga o sistema (vigia + painel + Gestão) junto com o Windows, pelo Agendador de Tarefas.
# Uso: agendar.ps1            -> instala/atualiza a tarefa e (re)liga agora
#      agendar.ps1 -Reiniciar -> só reinicia agora (continua ligando sozinho)
#      agendar.ps1 -Remover   -> desliga e para de ligar sozinho
param([switch]$Remover, [switch]$Reiniciar)
$ErrorActionPreference = "Stop"
$nome = "Painel Mariá - vigia"
$antigo = "Painel Pizzaria - atualizar dados"
$pasta = $PSScriptRoot
$script = Join-Path $pasta "vigia.py"

function Parar-Vigia {
    Stop-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue
    # o vigia pode ter se reiniciado sozinho depois de uma atualização (aí ele não é mais "da tarefa")
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "python*" -and $_.CommandLine -like "*vigia.py*" }
    foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
    Start-Sleep -Seconds 2
}

Unregister-ScheduledTask -TaskName $antigo -Confirm:$false -ErrorAction SilentlyContinue
if ($Remover) {
    Parar-Vigia
    Unregister-ScheduledTask -TaskName $nome -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Sistema desligado. Ele não liga mais sozinho até você clicar em 'Ligar vigia e alertas'."
    exit 0
}
if ($Reiniciar) {
    if (-not (Get-ScheduledTask -TaskName $nome -ErrorAction SilentlyContinue)) {
        Write-Host "O sistema ainda não está instalado para ligar sozinho. Use 'Ligar vigia e alertas'."
        exit 1
    }
    Parar-Vigia
    Start-ScheduledTask -TaskName $nome
    Write-Host "Pronto! Sistema reiniciado. Abra em http://127.0.0.1:8766"
    exit 0
}

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
# 1) liga ao entrar no Windows; 2) a cada 5 minutos confere: se o sistema caiu, liga de novo
#    (se já estiver rodando, a segunda cópia percebe e fecha na hora, sem fazer nada)
$aoEntrar = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$vigiar = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
try { $vigiar.Repetition.Duration = "" } catch {}
$config = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
          -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Parar-Vigia
Register-ScheduledTask -TaskName $nome -Action $acao -Trigger @($aoEntrar, $vigiar) -Settings $config `
    -Description "Sistema da Mariá: acompanha os pedidos do Cardápio Web, avisa oportunidades e abre o painel e o Gestão em http://127.0.0.1:8766 (pasta $pasta)" -Force | Out-Null
Start-ScheduledTask -TaskName $nome

Write-Host ""
Write-Host "Pronto! O sistema está ligado e agora cuida de si mesmo:"
Write-Host " - liga sozinho quando você entra no Windows;"
Write-Host " - se cair, volta sozinho em poucos minutos;"
Write-Host " - quando tiver versão nova, se atualiza e se reinicia sozinho."
Write-Host ""
Write-Host "Abra em: http://127.0.0.1:8766  (atalho 'Mariá Gestão' na Área de Trabalho)"
Write-Host "Python usado: $pyw"
Write-Host "Registro do que ele faz: dados\vigia.log"
