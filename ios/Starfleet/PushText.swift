import Foundation

/// The pure parts of push, apart from UIKit so they run without a phone.
enum PushText {
    /// The device token as lower-case hex, the form Fleet stores and Apple's push service takes in a URL.
    static func hex(_ token: Data) -> String {
        token.map { String(format: "%02x", $0) }.joined()
    }

    /// "development" for a build Xcode signed for a device (it embeds a provisioning profile), else "production".
    static func environment() -> String {
        Bundle.main.path(forResource: "embedded", ofType: "mobileprovision") != nil ? "development" : "production"
    }

    /// "entitlement" when this build is not signed for push (a free Apple team: iOS reports that there is no
    /// valid aps-environment entitlement, NSCocoaErrorDomain 3000), otherwise a short text.
    static func errorText(_ error: Error) -> String {
        let nsError = error as NSError
        let text = nsError.localizedDescription
        if (nsError.domain == NSCocoaErrorDomain && nsError.code == 3000) || text.localizedCaseInsensitiveContains("aps-environment") {
            return "entitlement"
        }
        return String(text.prefix(120))
    }

    /// `PADD.<fn>(<arguments>)` for evaluateJavaScript, or nil when an argument cannot be written as JSON.
    /// Arguments go through JSONSerialization, never into the script as pasted text. Guarded, so a page
    /// without that function does nothing. `fn` is always one of this app's own names, never page or push text.
    static func script(_ fn: String, _ args: [Any]) -> String? {
        var json: [String] = []
        for arg in args {
            guard JSONSerialization.isValidJSONObject([arg]),
                  let data = try? JSONSerialization.data(withJSONObject: arg, options: .fragmentsAllowed),
                  let text = String(data: data, encoding: .utf8) else { return nil }
            json.append(text)
        }
        return "window.PADD && typeof PADD.\(fn) === 'function' && PADD.\(fn)(\(json.joined(separator: ",")))"
    }
}
