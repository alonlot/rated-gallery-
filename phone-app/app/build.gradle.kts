import java.util.Properties

val onnxRuntimeVersion = "1.31.1"

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Signing: build_apk.sh creates keystore/release.jks and keystore/keystore.properties.
val keystorePropsFile = rootProject.file("keystore/keystore.properties")
val keystoreProps = Properties().apply {
    if (keystorePropsFile.isFile) keystorePropsFile.inputStream().use { load(it) }
}

android {
    namespace = "com.ratedgallery.phone"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.ratedgallery.phone"
        minSdk = 29
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        // ONNX Runtime ships native libraries for 4 ABIs (about 20 MB each). The app targets
        // 64-bit ARM phones (Poco F6), so only arm64-v8a is packaged.
        ndk {
            abiFilters += "arm64-v8a"
        }
    }

    signingConfigs {
        if (keystorePropsFile.isFile) {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile", "keystore/release.jks"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
        unitTests.all {
            // Search parity tests read the reference data of the PC side (paths relative to phone-app/).
            it.systemProperty("rg.testData", rootProject.file("../tests/data").absolutePath)
            it.systemProperty("rg.models", rootProject.file("../data/models").absolutePath)
            it.maxHeapSize = "2g"
        }
    }

    lint {
        abortOnError = true
        checkReleaseBuilds = false
        // targetSdk 35 is a requirement of this project (Poco F6, Android 14/15).
        disable += setOf("OldTargetApi", "GradleDependency", "AndroidGradlePluginVersion", "NewerVersionAvailable")
    }

}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

// Copy the shared web UI (offline gallery) from the PC server's static folder into assets/web/.
// A missing source folder is fine: nothing is copied, and the app falls back to assets/web-fallback/.
// Override the source with -PwebAssetsSrc=<dir> (used to test this task).
val webAssetsSrc: File = rootProject.file(providers.gradleProperty("webAssetsSrc").getOrElse("../ratedgallery/static"))
val webAssetsDest: File = layout.projectDirectory.dir("src/main/assets/web").asFile
val copyWebAssets = tasks.register<Copy>("copyWebAssets") {
    description = "Copies ../ratedgallery/static into app/src/main/assets/web"
    from(webAssetsSrc)
    into(webAssetsDest)
    // Mirror the source: remove files that were deleted there.
    doFirst { webAssetsDest.deleteRecursively() }
}
tasks.named("preBuild") {
    dependsOn(copyWebAssets)
    // Source folder gone or empty (copy skipped): drop an old copy so the fallback page is used.
    doFirst { if (webAssetsSrc.walk().none { it.isFile }) webAssetsDest.deleteRecursively() }
}
// Every task that reads the asset folder must run after the copy.
tasks.configureEach {
    if (name != "copyWebAssets" &&
        (name.contains("Assets") || name.startsWith("lint") || name.startsWith("generate") && name.contains("Lint"))
    ) {
        mustRunAfter(copyWebAssets)
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.webkit:webkit:1.12.1")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    implementation("androidx.lifecycle:lifecycle-process:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // Text search model (MIT license). Same Java API (ai.onnxruntime) as the desktop artifact used in tests.
    implementation("com.microsoft.onnxruntime:onnxruntime-android:$onnxRuntimeVersion")

    testImplementation("junit:junit:4.13.2")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
    // Real org.json for JVM tests (android.jar only has stubs).
    testImplementation("org.json:json:20240303")
    // Desktop ONNX Runtime (native libs for the JVM) to run the real text model in unit tests.
    testImplementation("com.microsoft.onnxruntime:onnxruntime:$onnxRuntimeVersion")
}

// Unit tests run on the JVM: use the desktop ONNX Runtime there, not the Android AAR (same classes, no JVM native libs).
configurations.matching { it.name.endsWith("UnitTestRuntimeClasspath") || it.name.endsWith("UnitTestCompileClasspath") }
    .configureEach { exclude(group = "com.microsoft.onnxruntime", module = "onnxruntime-android") }
