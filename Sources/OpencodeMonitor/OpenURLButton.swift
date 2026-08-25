import AppKit
import SwiftUI

/// SwiftUI's Link is known to not reliably register clicks inside a
/// MenuBarExtra popover or a non-activating NSPanel (this app uses both
/// for the dropdown and the desktop widget) -- drive NSWorkspace directly
/// from a Button instead of relying on Link's own gesture handling.
struct OpenURLButton: View {
    let title: String
    let url: URL

    var body: some View {
        Button {
            NSWorkspace.shared.open(url)
        } label: {
            Text(title)
                .foregroundStyle(.blue)
                .underline()
        }
        .buttonStyle(.plain)
    }
}
