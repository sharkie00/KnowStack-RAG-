param(
    [int]$Port = 8765,
    [switch]$Reset
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$serverArgs = @("server.py", "--port", "$Port")
if ($Reset) { $serverArgs += "--reset" }

Write-Host ""
Write-Host "  知栈 KnowStack · 操作系统笔记 RAG Demo" -ForegroundColor Cyan
Write-Host "  http://127.0.0.1:$Port" -ForegroundColor White
Write-Host "  Ctrl+C 停止" -ForegroundColor DarkGray
Write-Host ""

python @serverArgs
