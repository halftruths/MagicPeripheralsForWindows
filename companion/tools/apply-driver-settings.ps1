# Applies the AmtPtpHidFilter (Bluetooth) settings that fix stuck fingers and press-and-drag.
# Run from an elevated PowerShell, then power-cycle the trackpad: the driver only re-reads these
# when the trackpad reconnects (pnputil /restart-device was not enough in testing).
#
#   StopPressure / StopSize = 0xFFFFFFFF  Never freeze a contact on low pressure/size. The default
#                                         StopPressure=0 froze fingers reading 0 pressure mid-glide.
#   IgnoreButtonFinger = 0                The default froze every contact while the click was held,
#                                         which made press-and-drag impossible.
#
# To revert to driver defaults: .\apply-driver-settings.ps1 -Revert

param([switch]$Revert)

$key = 'HKLM:\SYSTEM\CurrentControlSet\Services\AmtPtpHidFilter\Parameters'
$names = 'StopPressure', 'StopSize', 'IgnoreButtonFinger'

if ($Revert) {
    foreach ($name in $names) { Remove-ItemProperty -Path $key -Name $name -ErrorAction SilentlyContinue }
    Write-Output 'Reverted to driver defaults. Power-cycle the trackpad to apply.'
    return
}

New-Item -Path $key -Force | Out-Null
New-ItemProperty -Path $key -Name StopPressure -PropertyType DWord -Value ([int32]-1) -Force | Out-Null
New-ItemProperty -Path $key -Name StopSize -PropertyType DWord -Value ([int32]-1) -Force | Out-Null
New-ItemProperty -Path $key -Name IgnoreButtonFinger -PropertyType DWord -Value 0 -Force | Out-Null
Get-ItemProperty -Path $key | Select-Object $names | Format-List
Write-Output 'Applied. Power-cycle the trackpad to apply.'
