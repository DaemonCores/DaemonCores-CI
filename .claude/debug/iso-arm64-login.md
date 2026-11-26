# ISO smoke: arm64 live never reaches login (tk-6eb23e)

## Symptom
Run 45691559463, job build-iso (arm64): no login prompt in 2400 s; the serial
log stops at guest 494.9 s on "ACPI: button: Power Button [PWRB]".

## Reproduction (proven)
QEMU 8.2.2 (localhost/qemu8-arm64), CI vmlinuz + initrd (artifact
iso-smoke-failure-arm64), live ISO rebuilt from debian-bootc:iso_arm64, same
QEMU flags as the workflow: same last line, no login.

## Tests
- CI kargs + console=ttyAMA0 (userspace output visible): systemd expects
  dev-ttyAMA0.device at guest 277 s, "Timed out waiting for device" at 367 s
  (90 s), then "Dependency failed for serial-getty@ttyAMA0.service".
- CI kargs + systemd.default_device_timeout_sec=900: "daemoncores-ve login:"
  on ttyAMA0 at guest 409 s; classify_boot_log.py prints "up".

## Cause
The udev coldplug under TCG outlasts the 90 s device default: the serial getty
is dropped for good, so a larger smoke budget alone could never help.

## Fix
Smoke kargs only: systemd.default_device_timeout_sec=${BUDGET}. The login
marker stays mandatory.

## Still to verify
The CI arm64 smoke after merge.
