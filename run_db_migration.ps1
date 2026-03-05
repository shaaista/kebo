$ErrorActionPreference = "Stop"

$DB_HOST = "172.16.5.32"
$DB_USER = "root"
$DB_NAME = "GHN_PROD_BAK"

$mysqlCmd = Get-Command mysql -ErrorAction SilentlyContinue
if (-not $mysqlCmd) {
    throw "mysql CLI not found in PATH. Install MySQL client or add mysql.exe to PATH."
}

$DB_PASS = Read-Host "Enter MySQL password"

$MigrationFiles = @(
    "migrations/2026_02_11_db_efficiency/00_precheck.sql",
    "migrations/2026_02_11_db_efficiency/01_backup.sql",
    "migrations/2026_02_11_db_efficiency/02_up.sql",
    "migrations/2026_02_11_db_efficiency/03_verify.sql"
)

function Invoke-MigrationFile {
    param([string]$FilePath)

    if (-not (Test-Path $FilePath)) {
        throw "Migration file not found: $FilePath"
    }

    $fullPath = (Resolve-Path $FilePath).Path.Replace("\", "/")
    Write-Host "Running: $FilePath" -ForegroundColor Cyan

    & mysql --host=$DB_HOST --user=$DB_USER "--password=$DB_PASS" $DB_NAME --execute="SOURCE `"$fullPath`";"

    if ($LASTEXITCODE -ne 0) {
        throw "Failed: $FilePath"
    }

    Write-Host "Done: $FilePath" -ForegroundColor Green
}

foreach ($file in $MigrationFiles) {
    Invoke-MigrationFile $file
}

Write-Host "All migration steps completed successfully." -ForegroundColor Green
