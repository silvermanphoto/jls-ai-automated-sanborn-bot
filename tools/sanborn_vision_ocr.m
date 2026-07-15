#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <Vision/Vision.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr, "Usage: sanborn_vision_ocr IMAGE [IMAGE ...]\n");
            return 2;
        }
        NSMutableArray *rows = [NSMutableArray array];
        for (int imageIndex = 1; imageIndex < argc; imageIndex++) {
            NSString *path = [NSString stringWithUTF8String:argv[imageIndex]];
            NSImage *image = [[NSImage alloc] initWithContentsOfFile:path];
            CGRect proposed = CGRectZero;
            CGImageRef cgImage = [image CGImageForProposedRect:&proposed context:nil hints:nil];
            if (image == nil || cgImage == nil) {
                fprintf(stderr, "Could not open image: %s\n", argv[imageIndex]);
                return 2;
            }
            VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
            request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
            request.usesLanguageCorrection = NO;
            request.recognitionLanguages = @[@"en-US"];
            request.minimumTextHeight = 0.005;
            VNImageRequestHandler *handler = [[VNImageRequestHandler alloc]
                initWithURL:[NSURL fileURLWithPath:path] options:@{}];
            NSError *error = nil;
            if (![handler performRequests:@[request] error:&error]) {
                fprintf(stderr, "Vision OCR failed for %s: %s\n", argv[imageIndex],
                        error.localizedDescription.UTF8String ?: "unknown error");
                return 1;
            }
            for (VNRecognizedTextObservation *observation in request.results) {
                VNRecognizedText *candidate = [[observation topCandidates:1] firstObject];
                if (candidate == nil) continue;
                CGRect box = observation.boundingBox;
                [rows addObject:@{
                    @"image": path,
                    @"text": candidate.string,
                    @"confidence": @(candidate.confidence),
                    @"x": @(box.origin.x),
                    @"y": @(box.origin.y),
                    @"width": @(box.size.width),
                    @"height": @(box.size.height),
                }];
            }
        }
        NSError *error = nil;
        NSData *json = [NSJSONSerialization dataWithJSONObject:rows
            options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:&error];
        if (json == nil) {
            fprintf(stderr, "Could not encode OCR output: %s\n",
                    error.localizedDescription.UTF8String ?: "unknown error");
            return 1;
        }
        fwrite(json.bytes, 1, json.length, stdout);
        fputc('\n', stdout);
    }
    return 0;
}
