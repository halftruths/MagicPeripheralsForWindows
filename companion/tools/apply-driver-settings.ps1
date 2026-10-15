# Applies the driver settings that fix stuck fingers and press-and-drag, for both connections:
#   Bluetooth: AmtPtpHidFilter (kernel filter), re-reads when the trackpad reconnects - power-cycle it.
#   USB:       AmtPtpDeviceUsbUm (user-mode driver), reads once when the device starts - unplug and re-plug
#              (its control-pipe reload only re-applies haptics in the v2.2 build).
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
# Ask the USB driver (if the trackpad is plugged in) to re-read its settings now: command 0x00 on its control pipe.
$usbReloaded = $false
try {
    $pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'AmtPtpControlPanelUsbUmInterface', 'InOut')
    $pipe.Connect(500); $pipe.WriteByte(0); $pipe.Flush()
    $resp = New-Object byte[] 2; [void]$pipe.Read($resp, 0, 2); $pipe.Dispose()
    $usbReloaded = $resp[0] -eq 0
} catch {}
Write-Output ($(if ($Revert) { 'Reverted to driver defaults.' } else { 'Applied.' }) +
              $(if ($usbReloaded) { ' USB: haptics reloaded; unplug and re-plug to apply the rest.' } else { ' USB: unplug and re-plug to apply (if wired).' }) +
              ' Bluetooth: power-cycle the trackpad to apply.')
