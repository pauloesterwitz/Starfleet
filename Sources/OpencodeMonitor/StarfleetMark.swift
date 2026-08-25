import AppKit
import SwiftUI

/// The classic Starfleet delta/arrowhead insignia, for the menu bar. A bundled
/// bitmap (embedded as base64 -- this SwiftPM project has no Xcode asset
/// catalog, and the file is ~2KB, small enough to keep in source), NOT a
/// SwiftUI Shape/Path: MenuBarExtra's label forces its content through
/// NSStatusItem's template-image pipeline, which does not reliably rasterize
/// raw Shape/Path vector content at all (confirmed live in an earlier design:
/// it silently fell back to a generic placeholder glyph instead of showing
/// anything). `isTemplate = true` makes AppKit tint it automatically for the
/// menu bar's light/dark appearance -- for the same reason, custom RGB colors
/// are NOT achievable here at all; the health indicator next to this mark
/// uses glyph choice, not color.
///
/// Geometry transcribed from the community vector recreation at
/// https://commons.wikimedia.org/wiki/File:Delta-shield.svg (its "ffb634"
/// foreground path: a single closed loop, tip at top, two lower "wing" points
/// with a notch between) -- see assets/gen-starfleet-mark.swift for the exact
/// CoreGraphics drawing code used to build this bitmap, if it ever needs
/// regenerating at a different size.
enum StarfleetMark {
    static let image: NSImage = {
        let base64 = "iVBORw0KGgoAAAANSUhEUgAAADIAAABQCAYAAABbAybgAAAAAXNSR0IArs4c6QAAADhlWElmTU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAAqACAAQAAAABAAAAMqADAAQAAAABAAAAUAAAAAC18am8AAAE2UlEQVRoBd1aWahNURi+5ikZrkzJA/GAa8yUdG/hmiWzUm5mIV6EvCkPeBAKibx5o+RFIkoezFMUIVzTg2smmb9PZ5/WXf61195nT2v76++s/a9/+L691l577X12WVk6sgNluqdTKrkq1Uj9G3oyuRLJZ26DErVQEqEuhOZSDgO1R4K/ddDyvDGZqpHwCO3PE5HWAPvMQOQn7IPyQmaXgYQ3KhfyQGQIQP6wECGhmS6TaQRwVwOQIJF70MZQJ2UtUHnTJ8jvMhdZdACodyGJPId/C9fI7AtJwhsxjqIzUgEkQS5wD7z6y1Fp5gqT0wCiggvbXuUCEdMdPAwZ7seaZkmmAYrfgoYBbfKtyZLI/JhIkNztrIjwZnY/RiIkMyELMktiJkEiXDRSlSao9gRqmu9R7H3TZLIwIRI8AXvTIsKV6m6CRD4iN59nEpfpqBBl6gSJXZ04CxS4mAKRO0kTGZ0CCW/ERoYh0zCMM3zXhPSP4r4oSrBfbFd0fod6Zyzp3w+o1coPkNoXZkRWIDDNR1OuXLNVsHG0uTN9BU16FPT8Z+MAr+aYlQEJkvoF7aYCMbWDTq0aU4KE7bz5cocdi3RGllIfY/WpUsrx9SAsgozIAiTi+6qsZCAK97EVD0KkxpYkhf7Iq1cFQJYyHeKOsT492kaEq5UL0g8gevsBsRGJPKR+xUP2lfzSmxdY3FMkSr4rfsT9RsSVaeXhH4wG93ui+BGZKkZkZ+TNcXLY8h0RwO1BlKmQROxxExHTiIxHAM+AazIGgMQX3iYik1xjUMDD55MqCZtEhLZqydkRm4hNIjIAgNs7AlqCMVYySkQqJUeHbNw2ddLx5JEIFyFe9PVEJ0InvvJxXcTppYLm5iyJ9T/unA9U0Gzrb0WG6w4JHn9G7qPQG9DmUE6Xf6YMbJL0hLEL9KXUSRu/3In77On5+NjMb1XKobqMguEhVI+Rjufoweoxd5hSUFy2x8hPsH7CDw8uQ20195iS8Nb/LUACWwFT/wnkbmsqrtl5H7sDNeWi3fhSYqgl0C+prW8bcusrJEy+0gO9dVBTbr6+FT//WOwTZEpms/N6WAotVaYg0G8XLk7TnQiyAQvT/wX5ppXKQInjtWCqu07xKzZP+QSYEpnsH5Grspg5WqMlwu9DpVpHpNT8uEVyDmt7izwjpAIRbFWIlaYYCdaTdjgKC1jyf4M8fLZOQg4hqV7zJ2z1/kMZJjjpQbbjJEnwxJRDpVWM2ItLIpe6KPIOweOg16IkscSSxGbBp4I2b23n3qVU+YTAidCrpSYIEXcAvjc1//7qsTT/bFOJ/VxiK9VEKbSrUEPFdkateU7rVB1N7W+IyeRrHtQ9puCtRbsoj9AyAZbsXC3mFqPTb/RCSZ5IYuOyXNyqfC0YJdCSbRn8s5bdAOBh+3vBc1nzDEF+N2bNoFCf2/33BewzaCObIATos50BDgmXY+JaT0x8PRqEyEE6Oybch72A7uZ9hM++NjkOh+U2pwz6ufxvgXYjEV4jfnIenfOgXKlcFN4DuViVbYWaptYt9AV9PGWurKSKhfdDJSKPYe8KzYWYptZroOciwAspN8K9ijoi3ATyRUTu5BIQe0R426/OHYMC4LsFIlyVuDrlVp4COUdkZW4ZFIDzEXVD3kkQ/6b/gcQfx+g+ea/RGroAAAAASUVORK5CYII="
        let data = Data(base64Encoded: base64)!
        let img = NSImage(data: data)!
        img.isTemplate = true
        // Backing bitmap is 50x80 (4x a 12.5x20 logical box, for crispness) --
        // without setting `size` explicitly, NSImage/Image(nsImage:) displays
        // at native pixel dimensions instead of the intended menu-bar-icon size
        // (this exact mistake broke the previous mark's sizing).
        img.size = NSSize(width: 13, height: 20)
        return img
    }()
}

struct StarfleetMarkView: View {
    var body: some View {
        Image(nsImage: StarfleetMark.image)
            .frame(width: 13, height: 20)
    }
}
