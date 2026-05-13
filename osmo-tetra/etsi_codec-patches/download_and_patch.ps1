$URL = "http://www.etsi.org/deliver/etsi_en/300300_300399/30039502/01.03.01_60/en_30039502v010301p0.zip"
$MD5_EXP = "a8115fe68ef8f8cc466f4192572a1e3e"
$LOCAL_FILE = "etsi_tetra_codec.zip"
$PATCHDIR = Get-Location
$CODECDIR = Join-Path (Split-Path $PATCHDIR -Parent) "codec"

Write-Host "Deleting $CODECDIR ..."
if (Test-Path $CODECDIR) {
    Remove-Item -Recurse -Force $CODECDIR
}

Write-Host "Creating $CODECDIR ..."
New-Item -ItemType Directory -Path $CODECDIR | Out-Null

if (-not (Test-Path $LOCAL_FILE)) {
    Write-Host "Downloading $URL ..."
    Invoke-WebRequest -Uri $URL -OutFile $LOCAL_FILE
} else {
    Write-Host "Skipping download, file $LOCAL_FILE exists"
}

Write-Host "Checking MD5SUM ..."
$MD5 = (Get-FileHash -Algorithm MD5 $LOCAL_FILE).Hash.ToLower()
if ($MD5 -ne $MD5_EXP) {
    Write-Error "MD5sum of ETSI reference codec file doesn't match"
    exit 1
}

Write-Host "Unpacking ZIP ..."
Set-Location $CODECDIR
Expand-Archive -Path (Join-Path $PATCHDIR $LOCAL_FILE) -DestinationPath $CODECDIR

Write-Host "Applying Patches ..."
$series = Get-Content (Join-Path $PATCHDIR "series")
foreach ($p in $series) {
    Write-Host "=> Applying patch '$p'..."
    & "C:\Program Files\Git\usr\bin\patch.exe" -p1 -i (Join-Path $PATCHDIR $p)
}

Write-Host "Done!"