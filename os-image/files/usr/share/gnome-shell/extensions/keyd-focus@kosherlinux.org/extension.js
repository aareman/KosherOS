// Tells keyd-application-mapper which window is in front, one line per
// change ("class<TAB>title"), through $XDG_RUNTIME_DIR/keyd.fifo, the
// pipe the mapper reads (kosher-keyd-apps.service makes it). Based on
// keyd's own GNOME extension (MIT), with two differences: this one does
// not start or kill the mapper, which is the user unit's job, and it
// never creates the pipe's path, so with no mapper it writes nothing.
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Kept across disable and enable. GNOME turns extensions off at the lock
// screen; closing the pipe then would hand the mapper an end of file,
// which it reads over and over.
let stream = null;
let opening = false;

function clean(s) {
    return (s ?? '').replace(/[\t\n]/g, ' ');
}

function open() {
    if (opening)
        return;
    const file = Gio.File.new_for_path(
        GLib.build_filenamev([GLib.get_user_runtime_dir(), 'keyd.fifo']));
    let info;
    try {
        info = file.query_info('standard::type',
            Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, null);
    } catch {
        return;
    }
    if (info.get_file_type() !== Gio.FileType.SPECIAL)
        return;
    opening = true;
    // Opening a pipe waits for its reader, so it is done off the main loop.
    file.append_to_async(Gio.FileCreateFlags.NONE, GLib.PRIORITY_DEFAULT, null,
        (f, res) => {
            opening = false;
            try {
                stream = f.append_to_finish(res);
            } catch {
                stream = null;
            }
        });
}

function send(line) {
    if (stream) {
        try {
            stream.write_all(new TextEncoder().encode(line), null);
            return;
        } catch {
            stream = null;
        }
    }
    open();
}

export default class KeydFocusExtension extends Extension {
    enable() {
        this._tracker = Shell.WindowTracker.get_default();
        this._focusId = this._tracker.connect('notify::focus-app', () => {
            const win = global.display.focus_window;
            send(win ? `${clean(win.get_wm_class())}\t${clean(win.get_title())}\n`
                : 'root\t\n');
        });
        // A system dialog (a password prompt) gets no app's keys.
        Main.layoutManager.connectObject('system-modal-opened',
            () => send(`system-modal\t${clean(global.stage.get_title())}\n`), this);
        open();
    }

    disable() {
        this._tracker.disconnect(this._focusId);
        this._tracker = null;
        Main.layoutManager.disconnectObject(this);
    }
}
