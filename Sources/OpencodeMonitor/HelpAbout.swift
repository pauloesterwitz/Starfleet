import AppKit
import SwiftUI

/// The Help window: Help menu (while the Fleet window is open) or Help in the menu bar
/// dropdown. Short on purpose; the README on GitHub is the full manual.
struct HelpView: View {
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                ForEach(helpSections.indices, id: \.self) { i in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(helpSections[i].title).font(.headline)
                        // LocalizedStringKey: **bold**, `code` and links; line breaks stay.
                        Text(LocalizedStringKey(helpSections[i].body))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .textSelection(.enabled)
            .padding(24)
            .frame(maxWidth: 600, alignment: .leading)
        }
        .frame(minWidth: 480, idealWidth: 600, minHeight: 420, idealHeight: 640)
    }
}

private let helpSections: [(title: String, body: String)] = [
    ("What it does", """
     The menu bar icon watches both Sparks, **Jean-Luc** and **Kathryn**, over SSH: GPU \
     load, temperature and power, the models llama-swap has loaded, and the coding-agent \
     sessions running on each. Click it for the details.
     **Open Starfleet Command…** opens the Fleet window, the page that manages the models: \
     load and pin them, Main Engineering, the NAS, the backup, the web server, Computer.
     **Show desktop widget** floats the same status on the desktop; **Send to USB panel** \
     drives the optional LCARS panel on the desk.
     """),
    ("The menu bar icon", """
     **?** in a circle: a node cannot be reached. Open the menu for the error.
     **⚠** triangle: a session is stalled and waits for you. A notification says which one.
     **⚡ 3**: three sessions are working right now.
     """),
    ("Keyboard", """
     **⌃⌥C**, from any app: brings Fleet to the front, opens Computer and starts voice \
     input. Press it again to stop recording.
     **⌘R**: reloads Fleet while its window is in front.
     The window's green button: Fleet in a full-screen Space of its own.
     In the page: **F** for the page's own full screen, **Esc** to leave it. \
     **Ctrl+Shift+Space** starts voice input in Computer while the window has focus; the \
     page lets you pick another combination.
     """),
    ("Fleet's address", """
     The Fleet window asks for Fleet's address the first time. Use the HTTPS address \
     `tailscale serve` publishes, `https://<head-node>.<tailnet>.ts.net:<port>`: the \
     microphone only works over HTTPS. A plain `http://` address loads too, without voice.
     When Fleet cannot be reached, the window shows the address again with **Retry**. To \
     change an address that works, run this in Terminal; the window then asks again:
     `defaults delete com.oesterwitz.starfleetcommand fleetURL`
     The Starfleet key stays in the page, as in a browser. The app never stores it.
     """),
    ("Updates", """
     At every start the app updates itself from the clone it was built from: it pulls \
     `main` from GitHub, rebuilds when the Mac app changed and starts again, and installs \
     the iPhone app when that changed. **Check for Updates…** in the menu bar dropdown \
     does the same on demand.
     A clone on another branch or with commits of its own is left alone. Building needs \
     Xcode in /Applications. The log is `~/Library/Logs/Starfleet Command update.log`.
     The iPhone needs to be on the cable or the same Wi-Fi for its install. With a free \
     Apple ID its app stops opening after 7 days: **Check for Updates…** then signs and \
     installs it again.
     """),
    ("When something is wrong", """
     **needs Tailscale re-auth**: click **Open sign-in page** and sign in again. Without \
     that button (check mode), run `ssh jean-luc` in Terminal and open the link it prints.
     **ssh exited 255** or **ssh call timed out**: is Tailscale on, is the node up, does \
     `ssh jean-luc` work in Terminal?
     **Can't reach Fleet**: the same checks for the head node, then **Retry**.
     **Microphone asked for again**: every rebuild gets a new ad-hoc signature, so macOS \
     may ask once more after an update.
     """),
    ("More", """
     The full manual is the README: \
     [github.com/pauloesterwitz/Starfleet](https://github.com/pauloesterwitz/Starfleet)
     """),
]

/// The standard About panel, with the build this app was made from and the credits.
enum AboutPanel {
    static let repoURL = URL(string: "https://github.com/pauloesterwitz/Starfleet")!

    /// The commit package.sh stamped into the bundle; nil for a `swift run` build.
    static var buildCommit: String? {
        Bundle.main.url(forResource: "build-commit", withExtension: nil)
            .flatMap { try? String(contentsOf: $0, encoding: .utf8) }
            .map { String($0.prefix(7)) }
    }

    static func show() {
        let link = "github.com/pauloesterwitz/Starfleet"
        let credits = NSMutableAttributedString(string: """
            Watches the Starfleet cluster from the menu bar and runs it from the Fleet window.

            Updates itself from GitHub at every start.
            \(link)

            USB panel protocol after James Buren's hidss (MIT).
            Star Trek and its iconography are trademarks of Paramount. \
            A personal, non-commercial project with no affiliation.
            """)
        // Without a font and colour the panel falls back to black Helvetica, unreadable in Dark Mode.
        let centered = NSMutableParagraphStyle()
        centered.alignment = .center
        credits.addAttributes([
            .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
            .foregroundColor: NSColor.secondaryLabelColor,
            .paragraphStyle: centered,
        ], range: NSRange(location: 0, length: credits.length))
        credits.addAttribute(.link, value: repoURL, range: (credits.string as NSString).range(of: link))

        // An LSUIElement app is never active by itself: without this the panel opens behind.
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [
            .credits: credits,
            .version: buildCommit.map { "build \($0)" } ?? "development build",
        ])
    }
}
