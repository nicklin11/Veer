plugins {
    id("com.android.application")
}

android {
    namespace = "com.veer.wheel"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.veer.wheel"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        debug {
            isDebuggable = true
            buildConfigField("boolean", "DEBUG_OVERLAY", "false")
        }
        release {
            isMinifyEnabled = false
            buildConfigField("boolean", "DEBUG_OVERLAY", "false")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
}
