param([string]$OutputDirectory = 'go-backend/dist/g9-release', [switch]$ManifestOnly)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskOutput = [IO.Path]::GetFullPath((Join-Path $taskRoot $OutputDirectory))
if (-not $taskOutput.StartsWith((Join-Path $taskRoot 'go-backend/dist') + [IO.Path]::DirectorySeparatorChar)) { throw 'Output must remain in go-backend/dist' }
New-Item -ItemType Directory -Force -Path $taskOutput | Out-Null
Push-Location $taskRoot
try {
    $revision = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'git revision unavailable' }
    $dirty = if (git status --porcelain) { 'true' } else { 'false' }
    $builtAt = [DateTime]::UtcNow.ToString('o')
    $frontendBuild = Join-Path $taskRoot 'frontend/.next/BUILD_ID'
    if (-not (Test-Path -LiteralPath $frontendBuild)) { throw 'Build frontend before packaging release' }
    $suffix = if ($IsWindows -or $env:OS -eq 'Windows_NT') { '.exe' } else { '' }
    $flags = "-s -w -X agentevalops/go-backend/internal/buildinfo.Revision=$revision -X agentevalops/go-backend/internal/buildinfo.Dirty=$dirty -X agentevalops/go-backend/internal/buildinfo.BuiltAt=$builtAt"
    $binaries = @{}
    Push-Location (Join-Path $taskRoot 'go-backend')
    try {
        foreach ($binary in @('api', 'worker', 'evalgate')) {
            $target = Join-Path $taskOutput ($binary + $suffix)
            if (-not $ManifestOnly) {
                go build -trimpath -ldflags $flags -o $target "./cmd/$binary"
                if ($LASTEXITCODE -ne 0) { throw "build failed: $binary" }
            }
            $identity = (& $target --version | ConvertFrom-Json)
            if ($LASTEXITCODE -ne 0 -or $identity.git_sha -ne $revision -or $identity.dirty -ne $dirty) { throw "identity mismatch: $binary" }
            if ($identity.schema_head -ne 'c12a00800001' -or $identity.built_at -eq 'unknown') { throw "unverified metadata: $binary" }
            if ($ManifestOnly -and $binary -ne 'api' -and $identity.built_at -ne $builtAt) { throw 'binary build timestamps differ' }
            $builtAt = $identity.built_at
            $binaries[$binary] = @{ sha256 = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower(); version = $identity }
        }
    } finally { Pop-Location }
    $sources = @{}
    foreach ($file in (git ls-files --cached --others --exclude-standard -- go-backend frontend scripts/release-g9.ps1 docker-compose.go-rehearsal.yml .dockerignore .github/workflows/go-product-release.yml)) {
        if (Test-Path -LiteralPath $file -PathType Leaf) { $sources[$file] = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower() }
    }
    $evidence = @{}
    $evidenceDir = Join-Path $taskRoot 'go-backend/dist/g9-evidence'
    if (Test-Path -LiteralPath $evidenceDir) {
        foreach ($file in (Get-ChildItem -LiteralPath $evidenceDir -File)) { $evidence[$file.Name] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower() }
    }
    $containerFile = Join-Path $evidenceDir 'container-identities.json'
    $containers = if (Test-Path -LiteralPath $containerFile) { Get-Content -Encoding UTF8 -LiteralPath $containerFile -Raw | ConvertFrom-Json } else { $null }
    $manifest = @{ classification = 'CONTROLLED_RELEASE_NOT_PRODUCTION_VALIDATED'; git_sha = $revision; dirty = $dirty; built_at = $builtAt; go_version = (go version); node_version = (node --version); schema_head = $binaries.api.version.schema_head; frontend_build_id = ([IO.File]::ReadAllText($frontendBuild)).Trim(); binaries = $binaries; container_images = $containers; source_sha256 = $sources; evidence_sha256 = $evidence }
    [IO.File]::WriteAllText((Join-Path $taskOutput 'release-manifest.json'), ($manifest | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    Write-Output "Verified release manifest: $taskOutput"
} finally { Pop-Location }
