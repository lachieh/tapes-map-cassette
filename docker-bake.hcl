variable "TAG" {
  default = "local"
}

target "image" {
  context    = "."
  dockerfile = "Dockerfile"
  platforms  = ["linux/amd64", "linux/arm64"]
  tags       = ["ghcr.io/lachieh/tapes-map-cassette:${TAG}", "ghcr.io/lachieh/tapes-map-cassette:rolling"]
  args = {
    VERSION = TAG == "local" ? "0.0.0" : trimprefix(TAG, "v")
  }
  labels = {
    "org.opencontainers.image.source" = "https://github.com/lachieh/tapes-map-cassette"
    "org.opencontainers.image.version" = "${TAG}"
  }
}

group "default" {
  targets = ["image"]
}
