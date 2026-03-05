$ErrorActionPreference = "Stop"

$DB_HOST = "172.16.5.32"
$DB_USER = "root"
$DB_NAME = "GHN_PROD_BAK"
$ROLLBACK_FILE = "migrations/2026_02_11_db_efficiency/04_down.sql"

$mysqlCmd = Get-Command mysql -ErrorAction SilentlyContinue
if (-not $mysqlCmd) {
    throw "mysql CLI not found in PATH. Install MySQL client or add mysql.exe to PATH."
}

if (-not (Test-Path $ROLLBACK_FILE)) {
    throw "Rollback file not found: $ROLLBACK_FILE"
}

$DB_PASS = Read-Host "Enter MySQL password"
$fullPath = (Resolve-Path $ROLLBACK_FILE).Path.Replace("\", "/")

Write-Host "Running rollback: $ROLLBACK_FILE" -ForegroundColor Yellow
& mysql --host=$DB_HOST --user=$DB_USER "--password=$DB_PASS" $DB_NAME --execute="SOURCE `"$fullPath`";"

if ($LASTEXITCODE -ne 0) {
    throw "Rollback failed."
}

Write-Host "Rollback completed successfully." -ForegroundColor Green
