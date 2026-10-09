# Try KosherOS on a spare computer

Use a Windows PC or a Mac to prepare an installer USB, then install KosherOS on a separate Intel or AMD PC. You do not need to know Linux commands to follow these steps once you have the installer file.

!!! warning "Early testing only"

    KosherOS is pre-alpha. Use a spare computer and back up anything you want to keep. Installation replaces the operating system and erases the selected destination disk. Preparing the USB also erases everything on that USB.

!!! info "Installer availability"

    The latest release checked for this guide, v0.9.1, has no downloadable installer attached. You will need a KosherOS installer supplied by the project before continuing. Check the [official releases](https://github.com/aareman/KosherOS/releases) for a file ending in `.iso`. GitHub's “Source code” downloads are not installers. If there is no ISO, [ask about testing](https://github.com/aareman/KosherOS/issues/new). Developers can use the [build instructions](contributing.md#the-loops).

## System requirements

These are **provisional planning requirements for early testing**, not a tested minimum or a guarantee of compatibility. The recommended column gives more room for local picture checking, browser tabs, apps and updates. We still need complete installation and everyday-use tests on machines at the minimum.

| Part | Minimum to plan an early test | Recommended for family testing |
|---|---|---|
| Computer | Intel or AMD PC with a 64-bit x86_64 processor and at least 2 cores | A recent Intel or AMD x86_64 PC with at least 4 cores |
| Memory (RAM) | 4 GB | 8 GB or more; consider 16 GB for heavier multitasking |
| Internal storage | A disk with at least 40 GB capacity dedicated to KosherOS | A 128 GB or larger SSD |
| Graphics | Graphics supported by Fedora's GNOME desktop | Supported integrated graphics are a reasonable starting point; the advanced desktop needs working GPU acceleration |
| Startup firmware | USB boot with UEFI or legacy BIOS | UEFI |
| Network | Internet access for obtaining the installer, updates and online apps | Ethernet for initial setup if Wi-Fi support is uncertain |

**Apple Silicon and other ARM computers are not supported by the current KosherOS image.** This guide targets an Intel/AMD PC, not installation on a Mac. A Mac, including an Apple Silicon Mac with a compatible USB-writing app, can be the computer you use to prepare the USB for that PC.

Fedora Workstation lists 4 GB RAM and 40 GB storage as its recommended baseline. Our provisional lower column uses that baseline; it does not establish that all KosherOS workloads fit comfortably. The larger recommendation is planning headroom, not a measured performance threshold. See [Fedora's hardware guidance](https://fedoraproject.org/workstation/download/).

KosherOS's [picture-filter measurements](media-filtering.md#running-on-the-familys-own-computer) show that checking can be slow on two cores. When the filter cannot keep up, it hides pictures instead of checking and displaying them, and reports the condition in Protection. Core count alone does not guarantee a particular speed. Check [supported hardware and tools](supported.md#hardware) for further limits.

## What you need

- A spare Intel/AMD computer that meets the provisional requirements above, with its power supply.
- A Windows PC or Mac with internet access to prepare the USB. This can be a different computer from the one receiving KosherOS.
- An empty USB drive. Plan on 16 GB or larger, **but check the installer file size and the release's requirements before buying or using one**. No final minimum USB capacity has been verified for a published installer yet; the image must fit on the drive. A 32 GB USB 3 drive gives more headroom.
- The official KosherOS `.iso` installer file. An ISO is the file the USB-writing app turns into an installer.
- A backup of every file you want to keep from the spare computer and the USB drive.

This is an installation guide, not a way to try a live desktop without changing the spare computer. It also does not cover installing alongside Windows.

## 1. Get the installer

Open [KosherOS releases](https://github.com/aareman/KosherOS/releases) and read the notes for the build you intend to test. If an installer is supplied, download its **x86_64 ISO** and follow any verification instructions supplied with that release. Keep it in Downloads so you can find it easily.

If the release has no ISO, stop here and request one from the project. Do not substitute the standard Fedora installer: it installs Fedora, not KosherOS.

## 2. Install the USB-writing app

Use [Fedora Media Writer from its official releases](https://github.com/FedoraQt/MediaWriter/releases). It can write a custom ISO, including one you already downloaded. See [Fedora's custom-image instructions](https://fedoraproject.org/wiki/User:Pfrields/FMW_instructions_for_other_ISO).

### On Windows

Download the Windows installer for Fedora Media Writer, open it, and follow its setup prompts. Then open Fedora Media Writer from the Start menu.

### On a Mac

Download the macOS disk image for your Mac's processor: Apple Silicon/ARM64 or Intel/x64. Open the `.dmg`, copy Fedora Media Writer into Applications, and open it there. Check the app's [current macOS requirements](https://github.com/FedoraQt/MediaWriter/blob/main/MAC.md) if it does not run on your Mac.

The USB-writing app must match the Mac you are using. **The KosherOS ISO must still be x86_64 for the separate Intel/AMD PC.**

## 3. Write the USB

1. Disconnect other removable drives so they cannot be selected by mistake. Insert the empty USB you intend to erase.
2. In Fedora Media Writer, choose the option to use a **custom image** or a file already on your computer. Wording can vary between versions.
3. Select the KosherOS `.iso` from Downloads. Do not select a Fedora edition from the catalogue.
4. Choose your USB by its name and capacity. Check this carefully: the selected USB will be erased.
5. Start writing and wait for the app to finish writing and checking the drive. Do not unplug it during this process.
6. Eject the USB safely. If Windows or macOS offers to format or initialize it afterwards, cancel: the installer has already been written.

Copying the ISO onto the USB in File Explorer or Finder does not create an installer. The USB-writing step is required.

## 4. Start the spare computer from the USB

1. Shut down the spare computer. Disconnect other external drives and connect its power supply.
2. Plug in the installer USB and turn on the computer.
3. Open the one-time boot menu using the key in your computer manufacturer's instructions. It is often F12, F9 or Esc, but varies by model. Search for your exact model and “boot menu” if unsure.
4. Choose the USB drive. If a UEFI entry is offered, use it on a UEFI computer.

If Windows starts, the computer booted from its internal disk. Restart and try the boot menu again. If the USB is not listed, try a different port and consult the manufacturer's USB-boot instructions. A Secure Boot rejection or a missing disk is a problem to report; do not change firmware security settings blindly.

## 5. Install on the spare computer

The KosherOS installer uses Fedora's Anaconda installer with a simplified setup screen.

**Check the installation destination before choosing “Begin Installation.”** When there is one eligible internal disk, the current installer selects it automatically; you may not get a separate disk-choice screen. Beginning installation erases that disk, including Windows and personal files.

If the computer has more than one eligible disk, open **Installation Destination**, identify the intended disk by its model and capacity, and review the selection. If you cannot confidently identify it, stop and ask for help. Do not select extra disks to make an error disappear.

After you confirm the destination and start installation, keep the computer plugged in and wait for completion. On restart, remove the USB when prompted or once installation has finished, so the computer boots from its internal disk.

## 6. Set up your family and test

The first start runs the KosherOS setup wizard. Follow it to create the administrator account and review the optional guardian password and device-security steps. Connect to your network when requested.

Start with a sample account and a few tasks: open the Store, try a site you normally use, review the account's settings in My Filter, and check Protection in the admin app. Add family accounts and choose their settings as you become comfortable with the system.

If something goes wrong, record the computer model, the KosherOS version shown in Updates or Settings → About, and what you did just before it happened. [Report your experience](https://github.com/aareman/KosherOS/issues/new); screenshots help, but remove passwords and personal information. See [reporting a problem](contributing.md#reporting-a-problem) for more detail.

## For developers

If you want to build an installer, run the apps without installing, or test in a virtual machine, use [developer setup and testing](contributing.md#setting-up). Those instructions remain separate from this USB guide.

## What has been verified

This guide follows the repository's installer configuration and first-boot flow, plus Fedora Media Writer's published instructions. The Windows/macOS USB-writing and physical installation sequence has **not yet been rehearsed end to end for a published KosherOS ISO**. The hardware figures above remain provisional until that testing is complete.
