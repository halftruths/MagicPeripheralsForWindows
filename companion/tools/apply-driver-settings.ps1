# Applies the driver settings that fix stuck fingers and press-and-drag, for both connections:
#   Bluetooth: AmtPtpHidFilter (kernel filter), re-reads when the trackpad reconnects - power-cycle it.
#   USB:       AmtPtpDeviceUsbUm (user-mode driver), reads once when the device starts - unplug and re-plug.
# (pnputil /restart-device was not enough for Bluetooth in testing.) Run from an elevated PowerShell.
#
#   StopPressure / StopSize = 0xFFFFFFFF  Never freeze a contact on low pressure/size. The default
#                                         StopPressure=0 froze fingers reading 0 pressure mid-glide.
#   IgnoreButtonFinger = 0                The default froze every contact while the click was held,
#                                         which made press-and-drag impossible.
#
# To revert to driver defaults: .\apply-driver-settings.ps1 -Revert

param([switch]$Revert)

$keys = 'HKLM:\SYSTEM\CurrentControlSet\Services\AmtPtpHidFilter\Parameters',
        'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\AmtPtpDeviceUsbUm\Parameters'
$names = 'StopPressure', 'StopSize', 'IgnoreButtonFinger'

foreach ($key in $keys) {
    if ($Revert) {
        foreach ($name in $names) { Remove-ItemProperty -Path $key -Name $name -ErrorAction SilentlyContinue }
        continue
    }
    New-Item -Path $key -Force | Out-Null
    New-ItemProperty -Path $key -Name StopPressure -PropertyType DWord -Value ([int32]-1) -Force | Out-Null
    New-ItemProperty -Path $key -Name StopSize -PropertyType DWord -Value ([int32]-1) -Force | Out-Null
    New-ItemProperty -Path $key -Name IgnoreButtonFinger -PropertyType DWord -Value 0 -Force | Out-Null
    Write-Output $key
    Get-ItemProperty -Path $key | Select-Object $names | Format-List
}
Write-Output ($(if ($Revert) { 'Reverted to driver defaults.' } else { 'Applied.' }) +
              ' Power-cycle the trackpad (Bluetooth) or unplug and re-plug it (USB) to apply.')
