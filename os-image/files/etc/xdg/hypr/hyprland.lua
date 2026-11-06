-- KosherOS: the advanced desktop on Hyprland, with the Noctalia shell.
--
-- Hyprland reads ~/.config/hypr/hyprland.lua if it exists and this file
-- otherwise, so this is what every account gets until it writes its own.
-- To customise, copy this file there and edit; it is reloaded on save.
--
--     mkdir -p ~/.config/hypr && cp /etc/xdg/hypr/hyprland.lua ~/.config/hypr/
--
-- Reference: https://wiki.hypr.land/Configuring/Start/
-- This is Hyprland's example configuration with the shell wired in, and
-- binds kept close to the niri session's, so moving between the two does
-- not mean learning new keys. Noctalia provides the bar, launcher,
-- notifications, lock screen, control centre, wallpaper and the polkit
-- agent. Nothing here touches filtering: that is per account at the
-- network layer, whatever draws the windows.

hl.monitor({
    output   = "",
    mode     = "preferred",
    position = "auto",
    scale    = "auto",
})

local terminal = "ptyxis"
local mainMod  = "SUPER"
local ipc      = "noctalia msg "

-- Session start: the systemd side of the session first, so the sign-in
-- helper (which seeds Noctalia's configuration on the first sign-in) and
-- the time-limit warnings run here as they do on the other desktops; then
-- the shell. Hyprland is not started as a systemd service, so the
-- environment is handed over before the target that stands for this
-- session is started.
hl.on("hyprland.start", function()
    hl.exec_cmd("systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP HYPRLAND_INSTANCE_SIGNATURE DISPLAY"
        .. " && systemctl --user start kosher-hyprland-session.target")
    hl.exec_cmd("noctalia")
end)
hl.on("hyprland.shutdown", function()
    hl.exec_cmd("systemctl --user --no-block stop kosher-hyprland-session.target")
end)

hl.config({
    general = {
        gaps_in     = 6,
        gaps_out    = 12,
        border_size = 3,
        col = {
            active_border   = "rgba(3b82f6ff)",
            inactive_border = "rgba(505050aa)",
        },
        resize_on_border = true,
        allow_tearing    = false,
        layout           = "dwindle",
    },

    decoration = {
        rounding       = 12,
        rounding_power = 2,
        active_opacity   = 1.0,
        inactive_opacity = 1.0,
        shadow = {
            enabled      = true,
            range        = 24,
            render_power = 3,
            color        = 0x77000000,
        },
        blur = {
            enabled  = true,
            size     = 3,
            passes   = 2,
            vibrancy = 0.1696,
        },
    },

    animations = {
        enabled = true,
    },

    dwindle = {
        preserve_split = true,
    },

    input = {
        -- English first and Hebrew ready, with Alt+Shift switching, the
        -- same as the system keymap the other desktops read. Hyprland does
        -- not read the system keymap, so the two layouts are named here.
        kb_layout  = "us,il",
        kb_options = "grp:alt_shift_toggle",
        numlock_by_default = true,
        follow_mouse = 1,
        touchpad = {
            natural_scroll = true,
            tap_to_click   = true,
        },
    },

    misc = {
        -- Hyprland's own wallpapers are not in the image; Noctalia draws
        -- the KosherOS one. Until it does, a plain colour.
        force_default_wallpaper  = 0,
        disable_hyprland_logo    = true,
        disable_splash_rendering = true,
        background_color         = 0x1f2933,
        -- Noctalia's launcher activates windows on the shell's behalf.
        focus_on_activate = true,
    },

    ecosystem = {
        no_update_news  = true,
        no_donation_nag = true,
    },
})

hl.gesture({
    fingers   = 3,
    direction = "horizontal",
    action    = "workspace",
})

-- Programs and the shell.
hl.bind(mainMod .. " + T",             hl.dsp.exec_cmd(terminal))
hl.bind(mainMod .. " + Space",         hl.dsp.exec_cmd(ipc .. "panel-toggle launcher"))
hl.bind(mainMod .. " + D",             hl.dsp.exec_cmd(ipc .. "panel-toggle launcher"))
hl.bind(mainMod .. " + S",             hl.dsp.exec_cmd(ipc .. "panel-toggle control-center"))
hl.bind(mainMod .. " + SHIFT + comma", hl.dsp.exec_cmd(ipc .. "settings-toggle"))
hl.bind("ALT + Tab",                   hl.dsp.exec_cmd(ipc .. "window-switcher hold"))
hl.bind(mainMod .. " + ALT + L",       hl.dsp.exec_cmd(ipc .. "session lock"))
hl.bind(mainMod .. " + Escape",        hl.dsp.exec_cmd(ipc .. "session lock"))

-- The screen reader, even on the lock screen.
hl.bind(mainMod .. " + ALT + S", hl.dsp.exec_cmd("pkill orca || exec orca"), { locked = true })

-- Media keys go through the shell so the on-screen display agrees.
hl.bind("XF86AudioRaiseVolume",  hl.dsp.exec_cmd(ipc .. "volume-up"),       { locked = true, repeating = true })
hl.bind("XF86AudioLowerVolume",  hl.dsp.exec_cmd(ipc .. "volume-down"),     { locked = true, repeating = true })
hl.bind("XF86AudioMute",         hl.dsp.exec_cmd(ipc .. "volume-mute"),     { locked = true })
hl.bind("XF86MonBrightnessUp",   hl.dsp.exec_cmd(ipc .. "brightness-up"),   { locked = true, repeating = true })
hl.bind("XF86MonBrightnessDown", hl.dsp.exec_cmd(ipc .. "brightness-down"), { locked = true, repeating = true })

-- Lock the screen when the lid closes.
hl.bind("switch:on:Lid Switch", hl.dsp.exec_cmd(ipc .. "session lock"), { locked = true })

-- Windows.
hl.bind(mainMod .. " + Q",         hl.dsp.window.close())
hl.bind(mainMod .. " + V",         hl.dsp.window.float({ action = "toggle" }))
hl.bind(mainMod .. " + F",         hl.dsp.window.fullscreen({ mode = "maximized" }))
hl.bind(mainMod .. " + SHIFT + F", hl.dsp.window.fullscreen({ mode = "fullscreen" }))
hl.bind(mainMod .. " + R",         hl.dsp.layout("togglesplit"))

-- Focus and move, with the arrows or with H J K L.
local directions = { left = "H", down = "J", up = "K", right = "L" }
for direction, letter in pairs(directions) do
    hl.bind(mainMod .. " + " .. direction,            hl.dsp.focus({ direction = direction }))
    hl.bind(mainMod .. " + " .. letter,               hl.dsp.focus({ direction = direction }))
    hl.bind(mainMod .. " + CTRL + " .. direction,     hl.dsp.window.move({ direction = direction }))
    hl.bind(mainMod .. " + CTRL + " .. letter,        hl.dsp.window.move({ direction = direction }))
    hl.bind(mainMod .. " + SHIFT + " .. direction,    hl.dsp.focus({ monitor = direction }))
end

-- Workspaces: Super and a number to go there, with Ctrl to take the
-- window along.
for i = 1, 9 do
    hl.bind(mainMod .. " + " .. i,          hl.dsp.focus({ workspace = i }))
    hl.bind(mainMod .. " + CTRL + " .. i,   hl.dsp.window.move({ workspace = i }))
end
hl.bind(mainMod .. " + Page_Down",  hl.dsp.focus({ workspace = "e+1" }))
hl.bind(mainMod .. " + Page_Up",    hl.dsp.focus({ workspace = "e-1" }))
hl.bind(mainMod .. " + mouse_down", hl.dsp.focus({ workspace = "e+1" }))
hl.bind(mainMod .. " + mouse_up",   hl.dsp.focus({ workspace = "e-1" }))

-- Move and resize with Super and the mouse.
hl.bind(mainMod .. " + mouse:272", hl.dsp.window.drag(),   { mouse = true })
hl.bind(mainMod .. " + mouse:273", hl.dsp.window.resize(), { mouse = true })

-- Leaving.
local quit = "command -v hyprshutdown >/dev/null 2>&1 && hyprshutdown || hyprctl dispatch 'hl.dsp.exit()'"
hl.bind(mainMod .. " + SHIFT + E", hl.dsp.exec_cmd(quit))
hl.bind("CTRL + ALT + Delete",     hl.dsp.exec_cmd(quit))

-- Noctalia's settings window, and password prompts and the like, float on
-- top of the tiled windows.
hl.window_rule({
    name  = "noctalia-settings",
    match = { class = "^dev\\.noctalia\\.Noctalia$" },
    float = true,
    size  = { 1080, 920 },
})
hl.window_rule({
    name  = "kosheros-dialogs",
    match = { class = "^(org\\.kosherlinux\\..*|org\\.gnome\\.Settings)$" },
    float = true,
})

-- Blur behind Noctalia's bar, panels, dock and notifications, without
-- Hyprland's own layer animations on top of the shell's.
hl.layer_rule({
    name  = "noctalia",
    match = {
        namespace = "^noctalia-(bar-.+|notification|dock|panel|attached-panel|osd|window-switcher)$",
    },
    no_anim      = true,
    ignore_alpha = 0.5,
    blur         = true,
    blur_popups  = true,
})
