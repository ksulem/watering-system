<#
  Retries launching the plant-monitor Ampere A1 Flex instance until Oracle
  has capacity. Cycles through all 3 Frankfurt ADs each pass, sleeps between
  passes. Safe to leave running for hours/days - stop with Ctrl+C.
#>

$OciExe        = "C:\oci-cli\Scripts\oci.exe"
$CompartmentId = "ocid1.tenancy.oc1..aaaaaaaaxreuts2ycwdkvtrkxakwjvrjbblfcbaqsjdjvn5ex74pb454iavq"
$SubnetId      = "ocid1.subnet.oc1.eu-frankfurt-1.aaaaaaaa2birrmivvnw4d62g2psc7cfn3uvr4ehnyqdca6o6dvfj3lerlhna"
$ImageId       = "ocid1.image.oc1.eu-frankfurt-1.aaaaaaaa4c6u6veu7nbkbmemmrumrmvx7d6ew7tivhwyrkhw7q3imc7yudnq"
$SshKeyFile    = "$HOME\.ssh\oci_plant_monitor.pub"
$DisplayName   = "plant-monitor-vm"
$Shape         = "VM.Standard.A1.Flex"
$RetryDelaySec = 60

# PowerShell mangles embedded double-quotes when passing JSON strings to
# native exes, so write the shape config to a file and reference it with
# file:// instead of passing it inline.
$ShapeConfigFile = Join-Path $PSScriptRoot "shape-config.json"
[System.IO.File]::WriteAllText($ShapeConfigFile, '{"ocpus": 1, "memoryInGBs": 6}', (New-Object System.Text.UTF8Encoding $false))
$ShapeConfig = "file://$ShapeConfigFile"

$AvailabilityDomains = @(
    "xTjm:EU-FRANKFURT-1-AD-1",
    "xTjm:EU-FRANKFURT-1-AD-2",
    "xTjm:EU-FRANKFURT-1-AD-3"
)

$LogFile = Join-Path $PSScriptRoot "oci-retry-launch.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

Log "Starting retry loop. Will try AD-1, AD-2, AD-3 every pass, sleep $RetryDelaySec s between passes."

$attempt = 0
while ($true) {
    foreach ($ad in $AvailabilityDomains) {
        $attempt++
        Log "Attempt #$attempt - trying $ad ..."

        $result = & $OciExe compute instance launch `
            --compartment-id $CompartmentId `
            --availability-domain $ad `
            --shape $Shape `
            --shape-config $ShapeConfig `
            --subnet-id $SubnetId `
            --image-id $ImageId `
            --ssh-authorized-keys-file $SshKeyFile `
            --display-name $DisplayName `
            --assign-public-ip true `
            --wait-for-state RUNNING `
            --max-wait-seconds 300 2>&1 | Out-String

        if ($LASTEXITCODE -eq 0) {
            Log "SUCCESS on $ad!"
            [console]::beep(1000, 400)
            [console]::beep(1500, 400)
            [console]::beep(2000, 600)

            $instanceId = ($result | ConvertFrom-Json).data.id
            Log "Instance OCID: $instanceId"

            Start-Sleep -Seconds 5
            $vnics = & $OciExe compute instance list-vnics --instance-id $instanceId 2>&1
            $publicIp = ($vnics | ConvertFrom-Json).data[0].'public-ip'
            Log "Public IP: $publicIp"
            Log "SSH with: ssh -i `"$HOME\.ssh\oci_plant_monitor`" ubuntu@$publicIp"
            exit 0
        }

        if ($result -match "Out of host capacity" -or $result -match "LimitExceeded" -or $result -match "TooManyRequests") {
            Log "$ad out of capacity, trying next AD..."
        } else {
            Log "UNEXPECTED ERROR on $ad :"
            Log $result
            Log "(continuing anyway - check above if this keeps repeating)"
        }
    }
    Log "All ADs failed this pass. Sleeping $RetryDelaySec s..."
    Start-Sleep -Seconds $RetryDelaySec
}
