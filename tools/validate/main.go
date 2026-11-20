// Command validate proves the map cassette is admissible: it checks that
// cassette.toml and the running server's x-tapes-cassette validate and share
// one digest, then runs tapes' own cassette runner against the server.
//
//	go run . ../../cassette.toml http://127.0.0.1:9997/openapi
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"

	"github.com/papercomputeco/tapes/api/cassetterunner"
	"github.com/papercomputeco/tapes/pkg/cassette"
	"github.com/papercomputeco/tapes/pkg/cassette/manifest"
	"github.com/papercomputeco/tapes/pkg/tapesoapi"
)

var contracts = []cassette.ContractVersion{"v1"}

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: validate <cassette.toml> <openapi url>")
		os.Exit(2)
	}
	if err := run(os.Args[1], os.Args[2]); err != nil {
		fmt.Fprintln(os.Stderr, "FAIL:", err)
		os.Exit(1)
	}
}

func run(tomlPath, url string) error {
	declared, err := manifest.Load(tomlPath)
	if err != nil {
		return fmt.Errorf("load %s: %w", tomlPath, err)
	}
	tomlDigest, err := validDigest(declared)
	if err != nil {
		return fmt.Errorf("%s: %w", tomlPath, err)
	}
	fmt.Println("cassette.toml       ", tomlDigest)

	served, err := servedManifest(url)
	if err != nil {
		return err
	}
	servedDigest, err := validDigest(served)
	if err != nil {
		return fmt.Errorf("x-tapes-cassette: %w", err)
	}
	fmt.Println("x-tapes-cassette    ", servedDigest)
	if tomlDigest != servedDigest {
		return fmt.Errorf("digests differ")
	}

	runner := cassetterunner.NewRunner(cassetterunner.Config{Contracts: contracts})
	runner.SetSources([]string{url})
	for _, refreshErr := range runner.Refresh(context.Background()) {
		return fmt.Errorf("tapes refused the cassette: %w", refreshErr)
	}
	if rejections := runner.Registry().Rejections(); len(rejections) > 0 {
		return fmt.Errorf("tapes rejected the cassette: %+v", rejections)
	}
	instance, ok := runner.Registry().Get(declared.CassetteName())
	if !ok {
		return fmt.Errorf("tapes admitted no cassette named %q", declared.CassetteName())
	}
	spec, _, _ := runner.Spec(instance.Name)
	var republished struct {
		Paths map[string]map[string]any `json:"paths"`
	}
	if err := json.Unmarshal(spec, &republished); err != nil {
		return fmt.Errorf("republished document: %w", err)
	}
	paths := make([]string, 0, len(republished.Paths))
	for path, operations := range republished.Paths {
		for method := range operations {
			paths = append(paths, method+" "+path)
		}
	}
	sort.Strings(paths)
	fmt.Println("admitted            ", instance.Name, instance.Digest, "status", runner.Status(instance.Name))
	for _, path := range paths {
		fmt.Println("republished         ", path)
	}
	if instance.Digest != tomlDigest {
		return fmt.Errorf("admitted digest %s differs from cassette.toml", instance.Digest)
	}
	return nil
}

func validDigest(declared cassette.Manifest) (cassette.Digest, error) {
	if err := declared.Validate(contracts); err != nil {
		return "", err
	}
	return declared.Digest()
}

func servedManifest(url string) (cassette.Manifest, error) {
	response, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, err
	}
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET %s: %s", url, response.Status)
	}
	document, err := tapesoapi.Parse(body)
	if err != nil {
		return nil, fmt.Errorf("parse OpenAPI: %w", err)
	}
	served, present, err := manifest.FromDocument(document)
	if err != nil {
		return nil, err
	}
	if !present {
		return nil, fmt.Errorf("%s has no %s extension", url, manifest.Extension)
	}
	return served, nil
}
