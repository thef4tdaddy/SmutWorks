# Duplicate review and cleanup

Open **Duplicates → Find duplicate groups**. Every scan is read-only and saves `duplicate-review.txt` and `duplicate-review.json` in the reports directory. Reports contain private paths and metadata; sanitize them before sharing.

## Detection methods

- **Multiple files on one scene:** Stash already associates several files with one scene.
- **Perceptual hash:** calls Stash's native duplicate-scene query with configurable hash distance and duration difference. Distance defaults to 0; larger distances make broader, less certain matches.
- **Exact fingerprints:** groups matching MD5/SHA256 fingerprints already stored by Stash. It does not read and hash your entire video library. Missing fingerprints cannot match this way.
- **Same Stash IDs, URLs, or titles:** uses Stash's duplicate filters and groups exact identifiers/URLs or normalized titles. These are review leads, not proof of identical content.
- **All methods:** combines overlapping groups and retains every match reason. It can take longer on a large library.

Matches include every attached file of the participating scenes, because a scene merge affects all of them. Files outside the configured incoming/library roots or unavailable locally block apply actions. Quarantine folders are excluded from discovery and encoding.

## Preferences

Duplicate review uses the same **Preferred quality profile** as encoding. Choose a default under **Duplicates → Duplicate review settings**, or change it for an individual group while reviewing.

| Preference | Recommendation |
| --- | --- |
| Best quality | Highest recorded resolution, then frame rate, known bit depth, and bitrate. This is a heuristic, not a measured quality score. |
| Closest to preferred quality | Closest available target resolution, HEVC/10-bit preference, then distance from target bitrate. |
| Smallest meeting preferred quality | Smallest file within the profile's codec, size, and bitrate limits while preserving the group's available target resolution. |
| Manual selection | No automatic keeper recommendation; inspect the files and choose a file ID. |

A larger bitrate does not necessarily mean better quality across different codecs or sources. Stash metadata does not expose all bit-depth/HDR information through this file query, so unknown bit depth is shown as unverified. Apply actions inspect the actual media with ffprobe. A recommended profile match that fails fresh verification requires manual override/review.

Differing or unknown durations block recommendations. Fresh duration discrepancies, HDR, interactive sidecars, changed files, and changed Stash records block file actions for manual review. Matching runtime still cannot prove that two edits are identical: view them before removing anything.

## Review actions

### Choose a primary file

Select a group, a file, and **Make a file primary**. For a single-scene group, this updates Stash's primary file and retains every video. History can undo the primary selection while files and assignments remain unchanged.

If the files are on separate scenes, use the merge action first. The destination is the scene containing your selected keeper.

### Consolidate scene metadata

**Merge scene metadata and choose primary** explicitly unions performers, tags, galleries, groups, URLs, and Stash IDs. It preserves compatible scalar/custom metadata and merges native play/o history. Source markers are moved by Stash. Conflicting titles, dates, studios, descriptions, ratings, custom fields, group positions, or resume positions block the merge until resolved in Stash. Legacy counters without corresponding timestamps also require manual review.

A private metadata snapshot is journaled before applying the native merge. The source scene records are removed; **their generated covers/previews may be removed by Stash**, while the keeper scene supplies the retained artwork. Media is retained. Merges cannot automatically recreate the old scene records. History undo restores only the primary selection, not an entire merge.

Custom-field merging requires a Stash API that supports `CustomFieldsInput.full`; otherwise the operation blocks rather than discarding those fields. Post-merge metadata and file assignments are checked before completion is recorded.

### Quarantine an unwanted copy

After choosing the primary, rescan. On a single-scene group, select **Quarantine one non-primary file**. This moves that one file through Stash's native move API into:

```text
<organized root>/smutworks quarantine/<unique action directory>/<original filename>
```

It remains attached in Stash, so its path stays synchronized. The primary cannot be quarantined. Files shared with scenes outside the group are blocked. Quarantine consumes the same space until you delete the file.

Rescan after each action. Native Stash update hooks can run; unexpected path/content changes leave a pending history entry for manual inspection.

### Restore or delete later

- **Review quarantine / restore** returns one recorded, unchanged file to its original location through Stash. Occupied destinations are never overwritten.
- **Delete one quarantined file** separately checks the keeper, file signatures, scene assignments, and quarantine location, then requires a typed confirmation containing the file ID. It calls Stash's native non-primary-file deletion API.

Deletion is permanent unless Stash has its own trash path configured. If Stash moves the file to trash, space may not be reclaimed until that trash is emptied. Estimates use logical file sizes and may overstate savings for hard links or deduplicated storage.

No scan, recommendation, or encoding run automatically deletes or quarantines a file. Duplicate changes share the encoding queue lock so they cannot run alongside that queue's encode process. Finish other external media-management operations before applying a review.

## Avoid redundant encodes

Build a duplicate report before an encoding queue. Both queue builders can omit a source when that report identifies another available, unchanged copy that passes fresh HEVC 10-bit/profile and duration checks.

Weak metadata-only matches and broad perceptual matches never suppress an encode. Cross-scene suppression additionally requires an exact file fingerprint/perceptual-hash pair, not simply membership in a connected group. Unknown bit depth, missing files, changed signatures, or a failed probe leave the source in the queue. Suppression saves processing; it does not remove any files.

## API compatibility

These features need Stash's native `findDuplicateScenes`, duplicate scene filters, video-file fingerprints/ownership, `sceneUpdate`, `sceneMerge`, `moveFiles`, and `deleteFiles` APIs. Unsupported schemas report an error; they do not fall back to filesystem-only mutation.

Implementation references:

- [Stash GraphQL query/mutation schema](https://github.com/stashapp/stash/blob/develop/graphql/schema/schema.graphql)
- [Scene and merge inputs](https://github.com/stashapp/stash/blob/develop/graphql/schema/types/scene.graphql)
- [Duplicate filters](https://github.com/stashapp/stash/blob/develop/graphql/schema/types/filters.graphql)
- [Native scene merge behavior](https://github.com/stashapp/stash/blob/develop/pkg/scene/merge.go)
- [Native file deletion guards](https://github.com/stashapp/stash/blob/develop/internal/api/resolver_mutation_file.go)
