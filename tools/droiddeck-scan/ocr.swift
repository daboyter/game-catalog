// ocr <image.png> → one JSON object per recognized text line:
//   {"text": "...", "x": left, "y": top, "w": width, "h": height, "conf": 0..1}
// Coordinates are in image pixels with the origin at the top-left (like adb input tap).
import Foundation
import Vision
import AppKit

let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read \(path)\n".data(using: .utf8)!)
    exit(1)
}
let W = Double(cg.width), H = Double(cg.height)
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
for obs in request.results ?? [] {
    guard let top = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox  // normalized, origin bottom-left
    let rec: [String: Any] = [
        "text": top.string,
        "x": Int(b.minX * W), "y": Int((1 - b.maxY) * H),
        "w": Int(b.width * W), "h": Int(b.height * H),
        "conf": Double(top.confidence),
    ]
    let data = try JSONSerialization.data(withJSONObject: rec)
    print(String(data: data, encoding: .utf8)!)
}
