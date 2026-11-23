# Keep the WebView JavaScript bridge (RGBridge). Methods are called by name from JavaScript.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
-keepattributes JavascriptInterface
-keep class com.ratedgallery.phone.GalleryBridge { *; }

# OkHttp ships its own consumer rules; these silence optional platform warnings.
-dontwarn okhttp3.internal.platform.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**

# ONNX Runtime (text search): the native library creates and calls these Java classes through JNI.
-keep class ai.onnxruntime.** { *; }
-dontwarn ai.onnxruntime.**
