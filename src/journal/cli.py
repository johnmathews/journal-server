"""CLI interface for the journal analysis tool."""

import argparse
import sys
from datetime import date
from pathlib import Path

from journal.config import load_config
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.logging import setup_logging
from journal.providers.embeddings import OpenAIEmbeddingsProvider
from journal.providers.extraction import AnthropicExtractionProvider
from journal.providers.ocr import build_ocr_provider
from journal.providers.transcription import build_transcription_provider
from journal.services.backfill import backfill_chunk_counts, rechunk_entries
from journal.services.chunking import build_chunker
from journal.services.chunking_eval import evaluate_chunking
from journal.services.entity_extraction import EntityExtractionService
from journal.services.ingestion import IngestionService
from journal.services.query import QueryService
from journal.vectorstore.store import ChromaVectorStore


def _build_services(config):
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )

    ocr = build_ocr_provider(config)
    transcription = build_transcription_provider(config)
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    chunker = build_chunker(config, embeddings)

    ingestion = IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=ocr,
        transcription_provider=transcription,
        embeddings_provider=embeddings,
        chunker=chunker,
        embed_metadata_prefix=config.chunking_embed_metadata_prefix,
        preprocess_images=config.preprocess_images,
    )
    query = QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=embeddings,
    )

    entity_store = SQLiteEntityStore(conn)
    extraction_provider = AnthropicExtractionProvider(
        api_key=config.anthropic_api_key,
        model=config.entity_extraction_model,
        max_tokens=config.entity_extraction_max_tokens,
    )
    entity_extraction = EntityExtractionService(
        repository=repo,
        entity_store=entity_store,
        extraction_provider=extraction_provider,
        embeddings_provider=embeddings,
        author_name=config.journal_author_name,
        dedup_similarity_threshold=config.entity_dedup_similarity_threshold,
        llm_candidate_top_k=config.entity_llm_candidate_top_k,
        llm_candidate_threshold=config.entity_llm_candidate_threshold,
        llm_match_min_cosine=config.entity_llm_match_min_cosine,
    )

    return ingestion, query, entity_extraction


def cmd_ingest(args, config):
    """Ingest a journal entry from an image or audio file."""
    ingestion, _, _ = _build_services(config)
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    data = file_path.read_bytes()
    entry_date = args.date or date.today().isoformat()

    # Detect source type from file extension
    ext = file_path.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
    audio_exts = {".mp3", ".m4a", ".wav", ".mp4", ".webm"}

    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    if ext in {".heic", ".heif"}:
        from journal.api import _convert_heic_to_jpeg

        data, media_type = _convert_heic_to_jpeg(data)

    if ext in image_exts:
        entry = ingestion.ingest_image(data, media_type, entry_date)
    elif ext in audio_exts:
        entry = ingestion.ingest_voice(data, media_type, entry_date, args.language)
    else:
        print(f"Error: Unsupported file type: {ext}", file=sys.stderr)
        sys.exit(1)

    print(f"Ingested entry {entry.id} for {entry.entry_date} ({entry.word_count} words)")
    print(f"Preview: {entry.final_text[:200]}...")


def cmd_search(args, config):
    """Search journal entries semantically."""
    _, query, _ = _build_services(config)
    results = query.search_entries(args.query, args.start_date, args.end_date, args.limit)

    if not results:
        print(f"No entries found matching '{args.query}'.")
        return

    for r in results:
        print(f"\n--- {r.entry_date} (relevance: {r.score:.0%}) ---")
        print(r.text[:300])
        if len(r.text) > 300:
            print(f"... ({len(r.text)} chars total)")


def cmd_list(args, config):
    """List journal entries."""
    _, query, _ = _build_services(config)
    entries = query.list_entries(args.start_date, args.end_date, args.limit)

    if not entries:
        print("No entries found.")
        return

    for e in entries:
        preview = e.final_text[:80].replace("\n", " ")
        print(f"{e.entry_date} | {e.source_type} | {e.word_count:>5} words | {preview}...")


def cmd_ingest_multi(args, config):
    """Ingest multiple page images as a single journal entry."""
    ingestion, _, _ = _build_services(config)

    images: list[tuple[bytes, str]] = []
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
    media_types_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }

    for file_str in args.files:
        file_path = Path(file_str)
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        ext = file_path.suffix.lower()
        if ext not in image_exts:
            print(f"Error: Unsupported image type: {ext}", file=sys.stderr)
            sys.exit(1)
        media_type = media_types_map.get(ext, "application/octet-stream")
        img_data = file_path.read_bytes()
        if ext in {".heic", ".heif"}:
            from journal.api import _convert_heic_to_jpeg

            img_data, media_type = _convert_heic_to_jpeg(img_data)
        images.append((img_data, media_type))

    entry_date = args.date or date.today().isoformat()
    entry = ingestion.ingest_multi_page_entry(images, entry_date)

    print(f"Ingested multi-page entry {entry.id} for {entry.entry_date}")
    print(f"  Pages: {len(images)}, Words: {entry.word_count}, Chunks: {entry.chunk_count}")
    print(f"  Preview: {entry.final_text[:200]}...")


def cmd_backfill_chunks(args, config):
    """Re-run the chunker over every entry and update its stored chunk_count.

    Fixes entries whose `chunk_count` is stale (e.g. seeded entries, or
    entries created before migration 0002 added the column). Does not touch
    the vector store — embeddings are not regenerated.
    """
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    # Backfill doesn't need embeddings, so pass None — SemanticChunker
    # would require one but we're intentionally using the configured
    # chunker (which, if semantic, would need embeddings; see WU-D for
    # the rechunk command that does full re-embedding).
    chunker = build_chunker(config, embeddings=None)
    result = backfill_chunk_counts(repo, chunker=chunker)

    print(f"Updated:   {result.updated}")
    print(f"Unchanged: {result.unchanged}")
    print(f"Skipped:   {result.skipped} (no text)")
    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  {err}")


def cmd_eval_chunking(args, config):
    """Measure chunking quality on the currently-stored corpus.

    Computes three numbers:
    - cohesion: sentences within a chunk are similar (higher = better)
    - separation: adjacent chunks within an entry are distinct (higher = better)
    - ratio: cohesion / (1 - separation), a single number to optimise

    No ground truth required. Re-run after `journal rechunk` to compare
    chunking configurations — higher ratio means better chunks.
    """
    import json

    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    result = evaluate_chunking(repo, vector_store, embeddings)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
        return

    print("Chunking quality (higher = better):")
    print(f"  Cohesion:   {result.cohesion:.3f}  (intra-chunk sentence similarity)")
    print(f"  Separation: {result.separation:.3f}  (inter-chunk distinctness)")
    print(f"  Ratio:      {result.ratio:.3f}  (cohesion / (1 - separation))")
    print()
    print(f"  {result.n_chunks_evaluated} chunks evaluated")
    print(f"  {result.n_entries_evaluated} entries evaluated")
    print(f"  {result.n_pairs_evaluated} adjacent chunk pairs evaluated")


def cmd_rechunk(args, config):
    """Re-run the FULL chunking + embedding pipeline over every entry.

    Unlike `backfill-chunks`, which only recomputes the `chunk_count`
    column, this command deletes each entry's existing vectors from
    ChromaDB and regenerates them using the currently-configured
    strategy. Use this when you've changed `CHUNKING_STRATEGY` or any
    semantic chunker parameter and want the stored chunks to match.

    With `--dry-run`, reports what would change without writing to
    ChromaDB or SQLite and without calling the embeddings API.
    """
    ingestion, _, _ = _build_services(config)
    repo = ingestion._repo  # type: ignore[attr-defined]

    result = rechunk_entries(ingestion, repo, dry_run=args.dry_run)

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}Updated:          {result.updated}")
    print(f"{prefix}Skipped:          {result.skipped} (no text)")
    print(f"{prefix}Old total chunks: {result.old_total_chunks}")
    print(f"{prefix}New total chunks: {result.new_total_chunks}")
    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  {err}")


def cmd_seed(args, config):
    """Seed the database with sample journal entries for development."""
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    samples = [
        # ── Chapter 1: A Long-expected Party ──
        {
            "date": "2025-06-10",
            "source_type": "photo",
            "text": (
                "The preparations for my party are well underway. I have decided "
                "that my one hundred and eleventh birthday shall be a truly "
                "magnificent affair — one that the Shire will remember for a very "
                "long time indeed. I have ordered pavilions and tents from Michel "
                "Delving, arranged for a tremendous quantity of food and drink from "
                "every corner of the four farthings, and invited what seems like "
                "half the population of Hobbiton and Bywater. Frodo is turning "
                "thirty-three on the same day, which makes it all the more special. "
                "He has been a splendid heir and companion these past years at Bag "
                "End. I do hope he will forgive me for what I am about to do."
            ),
        },
        {
            "date": "2025-06-15",
            "source_type": "photo",
            "text": (
                "Gandalf arrived this morning. I saw his cart coming up the Hill "
                "with that old grey horse of his, and my heart leapt. It has been "
                "far too long. We sat in the garden smoking pipe-weed and watching "
                "the sun set behind the Party Tree. He has brought the most "
                "extraordinary fireworks — rockets shaped like eagles and dragons, "
                "fountains of silver rain, and something he calls the special "
                "surprise that he will not let me see until the night itself. I "
                "told him about my plan. He listened very carefully, puffing on "
                "his pipe, and said nothing for a long while. Then he said I "
                "should think very carefully about the Ring. I told him I have "
                "thought about nothing else for months."
            ),
        },
        {
            "date": "2025-06-18",
            "source_type": "voice",
            "text": (
                "I am exhausted. Spent the whole day answering the door to "
                "well-wishers and delivery carts. The Sackville-Bagginses came "
                "round again — Lobelia had that look in her eye, the one she gets "
                "when she is mentally cataloguing my silver spoons. Otho stood "
                "behind her trying to look pleasant and failing entirely. I was "
                "perfectly civil but I confess I enjoyed telling them that no, the "
                "party was by invitation only, and yes, they were invited, but no, "
                "they could not bring their cousin from Hardbottle."
            ),
        },
        {
            "date": "2025-06-20",
            "source_type": "photo",
            "text": (
                "Went for a long walk today to clear my head. Took the path along "
                "the Water, past the mill, and up through the woods to the edge of "
                "the Bindbole Wood. The countryside is impossibly green this time "
                "of year. I sat on a stump for an hour and ate cheese sandwiches "
                "and thought about mountains. I miss the Lonely Mountain. I miss "
                "the sound of Dwarvish songs echoing in great halls. I miss the "
                "smell of adventure. Bag End is comfortable — perhaps too "
                "comfortable. I feel thin, like butter scraped over too much "
                "bread. I need a holiday."
            ),
        },
        {
            "date": "2025-06-22",
            "source_type": "photo",
            "text": (
                "Party day at last. I can hardly write, my hands are trembling — "
                "whether from excitement or nerves I cannot tell. The weather is "
                "perfect: warm sun, blue sky, a gentle breeze from the south. The "
                "great pavilion is up on the Party Field, the tables are laid for "
                "one hundred and forty-four guests at the special family dinner, "
                "and there are provisions for all the uninvited hobbits who will "
                "come anyway. Gandalf has been setting up his fireworks all "
                "morning and shooing away inquisitive hobbit-children. Frodo "
                "looks splendid in his new waistcoat. Everything is ready."
            ),
        },
        {
            "date": "2025-06-22",
            "source_type": "voice",
            "text": (
                "Recording this in the dark, walking fast along the road to "
                "Bucklebury. I did it. I actually did it. I stood up before all "
                "those hobbits, said my farewell speech — I do not think they "
                "understood a word of it — and then I slipped on the Ring and "
                "vanished. The look on their faces! Or rather, the look I imagine "
                "was on their faces, since I could not see them properly once the "
                "Ring was on. I crept away in the confusion and went straight back "
                "to Bag End. Gandalf was waiting for me. We had words about the "
                "Ring. He wanted me to leave it behind for Frodo. I did not want "
                "to. It is mine, I found it, it came to me. But in the end I "
                "left it on the mantelpiece in an envelope. I feel lighter already, "
                "in more ways than one. The road goes ever on and on."
            ),
        },
        {
            "date": "2025-06-24",
            "source_type": "photo",
            "text": (
                "Two days on the road now and the Shire is falling behind me. I "
                "slept under the stars last night near the Three-Farthing Stone "
                "and woke to birdsong and dew on my blanket. I have packed light "
                "— just my old travelling cloak, my walking stick, a few books, "
                "and some provisions. I left almost everything at Bag End for "
                "Frodo. The spoons, the furniture, the mathom collection, all of "
                "it. I wonder how long it will take the Sackville-Bagginses to "
                "realise they are not getting any of it. The thought makes me "
                "smile. I am heading for Rivendell first. Elrond will take me in. "
                "I have a book to write."
            ),
        },
        # ── Chapter 2: The Shadow of the Past (Gandalf returns) ──
        {
            "date": "2025-07-05",
            "source_type": "photo",
            "text": (
                "Arrived in Rivendell after a wonderfully uneventful journey. "
                "Elrond welcomed me as warmly as ever. I have a lovely room "
                "overlooking the falls, and a writing desk by the window where "
                "the light is good all morning. I have started work on my book — "
                "I am calling it There and Back Again, which Elrond says is a "
                "very hobbit sort of title. I suppose he means it as a "
                "compliment. The Elves here are endlessly courteous and slightly "
                "baffling. They sing at all hours and seem to think sleep is "
                "optional. I am adapting."
            ),
        },
        {
            "date": "2025-07-12",
            "source_type": "photo",
            "text": (
                "The writing is going well. I have drafted three chapters about "
                "the Unexpected Party — when Gandalf and all those Dwarves showed "
                "up at Bag End and turned my life upside down. Balin, Dwalin, "
                "Fili, Kili, Oin, Gloin, Dori, Nori, Ori, Bifur, Bofur, Bombur, "
                "and of course Thorin Oakenshield himself. I can still hear them "
                "singing about smashing my plates and bending my forks. Those were "
                "the days. I had no idea what I was getting into. I wonder how "
                "Frodo is managing at Bag End. I hope Gandalf is keeping an eye "
                "on him. There is something about that Ring that worries me still."
            ),
        },
        {
            "date": "2025-07-15",
            "source_type": "voice",
            "text": (
                "Could not sleep tonight. Kept thinking about the Ring. It has "
                "been weeks since I gave it up but I still reach for my pocket "
                "sometimes, expecting to feel it there. Gandalf was right to make "
                "me leave it behind, I know that in my head, but my heart is "
                "slower to agree. I dreamed of dark tunnels and a pale creature "
                "with enormous eyes. Gollum. I have never told the true story of "
                "how I got the Ring — I told the Dwarves and Gandalf a version "
                "where Gollum gave it to me as a present. That was a lie. I won "
                "it in a riddle game, fair and square, but I took it when he was "
                "not looking. Gandalf seemed to know this already."
            ),
        },
        {
            "date": "2025-07-20",
            "source_type": "photo",
            "text": (
                "A quiet week of writing and walking in the valley. Elrond's "
                "library is magnificent — scrolls and books in every language of "
                "Middle-earth, some so old the parchment crumbles if you breathe "
                "on it too hard. I found an account of the Last Alliance written "
                "by an Elf who was actually there, three thousand years ago. Three "
                "thousand years! I am one hundred and eleven and I feel ancient. "
                "These Elves make me feel like a child. Had a long talk with "
                "Arwen this evening about mortality and the passage of time. She "
                "understands more about that than most of her kind, I think."
            ),
        },
        {
            "date": "2025-07-28",
            "source_type": "photo",
            "text": (
                "News from the Shire via a trader who passed through Bree. "
                "Apparently my disappearance is still the talk of every pub "
                "from Hobbiton to Tuckborough. Various theories are circulating: "
                "I have gone mad, I have been murdered by Gandalf for my money, "
                "I am living in a cave somewhere counting my treasure. The "
                "Sackville-Bagginses have been trying to have Frodo declared "
                "legally incompetent so they can claim Bag End. Typical Lobelia. "
                "But Frodo is holding his own, the trader says, with help from "
                "Merry Brandybuck. Good lad, Merry."
            ),
        },
        {
            "date": "2025-08-03",
            "source_type": "voice",
            "text": (
                "Gandalf passed through Rivendell briefly today. He seemed "
                "preoccupied — more so than usual. He asked me many questions "
                "about the Ring, things he had asked before: how I found it, what "
                "it felt like to wear it, whether I ever saw writing on it. I "
                "told him about the letters that appear when you heat the Ring "
                "in a fire — strange angular script that glows red and then "
                "fades. He went very quiet when I said that. Then he said he had "
                "to leave immediately and rode off without even staying for "
                "supper. Very unlike Gandalf to miss a meal."
            ),
        },
        {
            "date": "2025-08-10",
            "source_type": "photo",
            "text": (
                "Finished the chapter about the trolls today — Tom, Bert, and "
                "William. I had forgotten how frightening that was at the time "
                "and how funny it seems now. Gandalf turning them to stone by "
                "imitating their voices until the sun came up. We found the troll "
                "hoard afterwards, which is where I got Sting. Dear little Sting, "
                "I gave it to Frodo along with the mithril coat. I hope he never "
                "needs either of them, but something tells me hope is not enough."
            ),
        },
        {
            "date": "2025-08-15",
            "source_type": "photo",
            "text": (
                "Rain all day. Stayed in my room and wrote about Rivendell — the "
                "first time I came here, sixty years ago, with Thorin and Company. "
                "How different it felt then, arriving exhausted and half-starved "
                "after our adventure with the trolls and the goblins and the "
                "wargs. Elrond read our map and found the moon-letters that "
                "revealed the secret entrance to the Lonely Mountain. Without "
                "that moment, the whole quest might have failed. Strange to be "
                "writing about this place while sitting in it. The falls sound "
                "the same. The food is just as good. Only I have changed."
            ),
        },
        # ── Chapter 3: Three is Company (Frodo's departure) ──
        {
            "date": "2025-09-01",
            "source_type": "photo",
            "text": (
                "Autumn is coming to Rivendell and the leaves in the valley are "
                "turning gold and copper. I have been working on the songs and "
                "poems for my book — translating some of the Dwarvish verses into "
                "Westron is proving tricky. Gloin's son Gimli sent me a letter "
                "with corrections to my account of the Battle of Five Armies. "
                "Apparently I got several Dwarvish clan names wrong. I do not mind "
                "the corrections but I wish he had been less smug about it."
            ),
        },
        {
            "date": "2025-09-10",
            "source_type": "voice",
            "text": (
                "Disturbing news. Gandalf sent word that he has confirmed his "
                "worst fears about the Ring. He did not say more in his letter — "
                "too dangerous, he wrote. But he said he has told Frodo everything "
                "and that Frodo has agreed to leave the Shire. My heart breaks for "
                "the lad. I carried that thing for sixty years without knowing "
                "what it truly was, and now Frodo must bear the consequences of "
                "my ignorance. Or perhaps not my ignorance — my wilful blindness. "
                "Part of me always knew there was something deeply wrong with it."
            ),
        },
        {
            "date": "2025-09-15",
            "source_type": "photo",
            "text": (
                "I cannot concentrate on my writing. I keep thinking about Frodo "
                "out there on the road with Sam. Gandalf's letter said Sam is "
                "going with him, which is a great comfort — there is no more "
                "loyal hobbit in the whole Shire than Samwise Gamgee. His father "
                "the Gaffer would be proud, in his grumbling way. I remember Sam "
                "as a little lad, peering over the garden fence with enormous "
                "eyes whenever I mentioned Elves. And now he is going to meet "
                "them. I hope the road is kind to them both."
            ),
        },
        {
            "date": "2025-09-18",
            "source_type": "photo",
            "text": (
                "Elrond found me sitting by the falls today, staring at nothing. "
                "He sat down beside me without speaking and we watched the water "
                "for a long time. Then he said that the choices of the Ring-bearer "
                "are not mine to make, and that guilt is a poor companion for an "
                "old hobbit. He is right, of course. He usually is. Three thousand "
                "years of wisdom will do that. I went back to my book and wrote "
                "a thousand words about the barrel-ride down the Forest River. It "
                "is easier to write about past adventures than to worry about "
                "present ones."
            ),
        },
        {
            "date": "2025-09-22",
            "source_type": "photo",
            "text": (
                "My birthday again — one hundred and twelve today, or is it one "
                "hundred and thirteen? I am losing count, which Gandalf says is "
                "a good sign. The Elves baked me a cake, which was very kind if "
                "somewhat oversized. Arwen sang a song in Quenya that she said "
                "was about the journeys of small folk who change the fate of the "
                "world. I pretended I was not crying. There has been no word from "
                "Frodo for weeks now. Gandalf is also silent. I trust them both "
                "but the silence is hard. I lit a candle for Frodo tonight and "
                "sat by the window watching the stars. Earendil was bright."
            ),
        },
        {
            "date": "2025-09-25",
            "source_type": "voice",
            "text": (
                "Finally, news. A Ranger from the north brought word that three "
                "hobbits were seen on the East Road near Bree. Three, not two — "
                "so someone else has joined Frodo and Sam. Merry or Pippin, I "
                "would wager, or both knowing those two. The Ranger also said "
                "there were dark riders on the road, tall figures on black horses "
                "that made his own horse shy and tremble. That worries me greatly. "
                "Elrond looked grave when I told him. He said he would send scouts."
            ),
        },
        {
            "date": "2025-09-30",
            "source_type": "photo",
            "text": (
                "The waiting is unbearable. I have written and rewritten the "
                "chapter about Smaug three times this week, not because it needed "
                "revision but because I need something to do with my hands and "
                "my mind. The great dragon, the desolation, the treasure hoard "
                "piled to the ceiling — and me, a small hobbit in the dark, "
                "talking to a monster. I was terrified but also, I confess, "
                "thrilled. That was the bravest thing I ever did, and the "
                "most foolish, which in my experience are usually the same thing."
            ),
        },
        {
            "date": "2025-10-05",
            "source_type": "photo",
            "text": (
                "Walked up to the high pass today, as far as the stone seats "
                "where you can see both east and west. The Misty Mountains are "
                "already capped with snow. Somewhere beyond them, Frodo is "
                "walking toward Mordor — or at least toward Rivendell, if Gandalf "
                "has any sense. The air was cold and thin and smelled of pine "
                "and stone. I am old. My knees ache on the steep parts and my "
                "breath comes shorter than it used to. But the view was worth "
                "every step. I could see the road winding away into the distance "
                "and I thought of all the roads I have walked and all the ones "
                "I never will."
            ),
        },
        {
            "date": "2025-10-10",
            "source_type": "voice",
            "text": (
                "They are coming. Elrond received word that Frodo is on his way "
                "to Rivendell. He was hurt — stabbed by a Morgul-blade on "
                "Weathertop. Aragorn is with him, Elrond says, and Glorfindel "
                "rode out to meet them. I am sick with worry. A Morgul-blade. I "
                "know enough Elvish lore to know what that means. If they do not "
                "reach Rivendell in time — but I cannot think about that. I must "
                "believe they will make it. Frodo is stronger than he looks. All "
                "hobbits are."
            ),
        },
        {
            "date": "2025-10-12",
            "source_type": "photo",
            "text": (
                "He is here. Frodo arrived last night, barely conscious, grey as "
                "ash. Elrond worked through the night to heal him. I sat outside "
                "the door and listened to the Elves singing healing songs and I "
                "thought my heart would break. This is my fault. I found the Ring, "
                "I kept it, I passed it on to him. If he dies it will be because "
                "of my foolishness. But Elrond says he will live. The splinter is "
                "removed. He is sleeping now, peacefully, for the first time in "
                "weeks. Sam is beside him, refusing to leave. Good Sam."
            ),
        },
        # ── The Council of Elrond and the Fellowship ──
        {
            "date": "2025-10-20",
            "source_type": "photo",
            "text": (
                "Frodo is recovering well. The colour is back in his cheeks and "
                "he ate two breakfasts this morning, which is the surest sign of "
                "hobbit health I know. We sat together in the garden and I gave "
                "him the mithril coat and Sting. He tried them on and looked so "
                "small and brave that I had to turn away for a moment. He told me "
                "everything — the Black Riders, the flight to Bucklebury Ferry, "
                "old Tom Bombadil in the forest, the barrow-wight. My dear boy "
                "has been through more in a few weeks than most hobbits see in "
                "a lifetime. And it is only the beginning."
            ),
        },
        {
            "date": "2025-10-25",
            "source_type": "voice",
            "text": (
                "The Council was today. Elrond summoned everyone — Gandalf, "
                "Aragorn, Legolas from the Woodland Realm, Gimli and Gloin from "
                "Erebor, Boromir from Gondor. I was there too, though I felt "
                "very small among such tall folk and great matters. They debated "
                "for hours about what to do with the Ring. Boromir wanted to use "
                "it as a weapon against Sauron. Gandalf said that was madness. "
                "Elrond said the Ring must be destroyed in Mount Doom where it "
                "was made. And then Frodo stood up and said he would take it. "
                "My heart stopped. I wanted to shout no, let someone else do it, "
                "someone stronger, someone who does not remind me of myself at "
                "thirty-three. But I said nothing. It was his choice to make."
            ),
        },
        {
            "date": "2025-11-02",
            "source_type": "photo",
            "text": (
                "The Fellowship is being assembled. Nine walkers to match the "
                "nine Riders, Elrond says. Frodo and Sam, of course. Gandalf. "
                "Aragorn, who turns out to be the heir of Isildur — I confess I "
                "did not see that coming. Legolas the Elf and Gimli the Dwarf, "
                "which should make for interesting company. Boromir of Gondor. "
                "And Merry and Pippin, bless them, who refused to be left behind. "
                "Elrond tried to dissuade them but Pippin said where Frodo goes "
                "we go, and that was that. I spent the evening with Frodo going "
                "over maps and telling him everything I remember about the Misty "
                "Mountains from my own crossing sixty years ago."
            ),
        },
        {
            "date": "2025-11-10",
            "source_type": "photo",
            "text": (
                "I have been writing furiously — trying to get as much of my book "
                "done as I can before the Fellowship departs. There is a feeling "
                "in Rivendell now, a sense of ending, as though we are all holding "
                "our breath before a great storm. The Elves sing differently — "
                "softer, sadder. Even the waterfall seems muted. I wrote about "
                "Mirkwood today. The spiders, the Elf-king's halls, the escape "
                "in the barrels. It reads like a children's adventure and I "
                "suppose in some ways it was. The world was simpler then, or "
                "perhaps I was simply too ignorant to see its complications."
            ),
        },
        {
            "date": "2025-11-18",
            "source_type": "voice",
            "text": (
                "Had a long talk with Gandalf tonight by the fire. He is worried, "
                "though he hides it well behind his pipe smoke and his riddles. "
                "He told me something about the Ring that chilled me. He said it "
                "wants to be found. That it has a will of its own and it betrayed "
                "Gollum just as it betrayed Isildur before him. That it came to "
                "me not by chance but by design — though whose design he cannot "
                "say. I asked him if it would betray Frodo too. He was quiet for "
                "a long time and then he said that depends entirely on Frodo."
            ),
        },
        # ── The Fellowship departs ──
        {
            "date": "2025-12-18",
            "source_type": "photo",
            "text": (
                "They are gone. The Fellowship left Rivendell this morning at "
                "dusk, heading south along the western bank of the Bruinen. I "
                "watched them go from the terrace — nine small figures against "
                "the twilight, with Gandalf's staff glowing faintly at the head "
                "of the line. Frodo turned and waved. I waved back and then I "
                "went inside and sat in my room and stared at the wall for a "
                "very long time. The house feels empty now. Even the Elves are "
                "quiet. I picked up my pen to work on the book but I could not "
                "write a single word. Tomorrow will be better."
            ),
        },
        {
            "date": "2025-12-25",
            "source_type": "photo",
            "text": (
                "Midwinter. Snow on the ground and ice on the falls. I have been "
                "trying to keep busy — writing, reading in the library, taking "
                "short walks when the weather permits. But my thoughts keep "
                "drifting south, following the Fellowship along paths I cannot "
                "see. Where are they now? Have they crossed the mountains? Are "
                "they safe? Elrond says worrying achieves nothing but I notice "
                "he spends a great deal of time staring south from the high "
                "balcony himself. Even Elves worry, it seems, when the stakes "
                "are high enough."
            ),
        },
        {
            "date": "2026-01-08",
            "source_type": "voice",
            "text": (
                "A brief message from Gandalf, sent by means he did not explain. "
                "The Fellowship reached Hollin safely but the pass over "
                "Caradhras was blocked by snow — or by darker forces, he "
                "suspects. They are considering the Mines of Moria instead. "
                "Moria. I remember Balin talking about his plan to recolonise "
                "Moria, years ago at the Unexpected Party. He was so full of "
                "hope. No one has heard from his colony in a long time. I "
                "have a bad feeling about this but I trust Gandalf's judgment. "
                "He has never led me wrong. Not permanently, anyway."
            ),
        },
        {
            "date": "2026-01-20",
            "source_type": "photo",
            "text": (
                "No word from the Fellowship for twelve days now. I tell myself "
                "this means nothing — they are underground in Moria where no "
                "messages can travel. But the silence gnaws at me. I have been "
                "rereading my account of the goblin tunnels under the Misty "
                "Mountains, where I found the Ring. Dark places underground. "
                "The riddle game with Gollum. What is in my pocket? The answer "
                "that saved my life and doomed — no, I must not think like that. "
                "I finished the chapter about the Battle of Five Armies today. "
                "Thorin's death. Even after sixty years it is hard to write "
                "about. He was a proud, stubborn, magnificent fool and I miss him."
            ),
        },
        {
            "date": "2026-02-01",
            "source_type": "photo",
            "text": (
                "Terrible news. Gandalf has fallen. The message came from "
                "Galadriel in Lothlorien — the Fellowship passed through Moria "
                "and there was a creature of fire and shadow, a Balrog, on the "
                "bridge of Khazad-dum. Gandalf stood against it and broke the "
                "bridge but the creature dragged him down into the abyss. He "
                "fell. Gandalf fell. I cannot believe it. I will not believe it. "
                "He has been a part of my life since I was fifty years old, "
                "since he came to my door with a mark scratched on it and "
                "thirteen Dwarves behind him. The world without Gandalf is a "
                "darker place and I am a smaller hobbit for his absence."
            ),
        },
        {
            "date": "2026-02-05",
            "source_type": "voice",
            "text": (
                "I have not written in my book for days. I sit at the desk and "
                "stare at the pages and all I can think about is Gandalf. The "
                "Elves have been very kind — they bring me food and tea and do "
                "not press me to talk. Arwen came and sat with me for an evening "
                "and told me stories about Gandalf that I had never heard, from "
                "ages before hobbits existed. He was old beyond imagining, she "
                "said, and his work in Middle-earth is not yet done. I am not "
                "sure what she meant by that but it gave me a strange comfort."
            ),
        },
        {
            "date": "2026-02-15",
            "source_type": "photo",
            "text": (
                "I have started writing again. Not the book — I cannot face that "
                "yet — but poetry. Elvish metres and Dwarvish rhythms and odd "
                "hobbit verses that do not quite fit either tradition. Elrond says "
                "grief makes poets of us all, which is a very Elvish thing to say. "
                "I wrote a song for Gandalf today. It is not very good but it "
                "made me feel better. I sang it to myself in the garden and a "
                "thrush on the wall tilted its head as though listening. Even "
                "the birds mourn him, I think."
            ),
        },
        {
            "date": "2026-02-28",
            "source_type": "photo",
            "text": (
                "Word from Lothlorien. The Fellowship rested there under "
                "Galadriel's protection for many days and has now set out again "
                "by boat down the Great River Anduin. Frodo is well, they say. "
                "He is grieving Gandalf but he has not turned back. Of course he "
                "has not. He is a Baggins and a Took and we do not turn back once "
                "we have set our feet on a path, however dark it grows. The "
                "company is making for Mordor by way of — well, I do not know "
                "by what way. There are many paths and none of them are safe."
            ),
        },
        # ── Spring and the breaking of the Fellowship ──
        {
            "date": "2026-03-05",
            "source_type": "voice",
            "text": (
                "Spring is coming to the valley. The snowmelt has swelled the "
                "Bruinen and the waterfalls are thundering day and night. Crocuses "
                "and snowdrops in the garden. Life goes on, as it always does, "
                "even when the world is breaking. I have gone back to the book. "
                "I am writing about Laketown and the coming of Smaug. The dragon "
                "descending on the wooden city, flames reflected in the water, "
                "people screaming, boats capsizing. And Bard with his black "
                "arrow. One arrow. One chance. He did not miss."
            ),
        },
        {
            "date": "2026-03-15",
            "source_type": "photo",
            "text": (
                "More news, and it is grim. The Fellowship has broken. At Amon "
                "Hen, above the falls of Rauros, Boromir tried to take the Ring "
                "from Frodo. Frodo fled and crossed the River alone — well, not "
                "alone. Sam went with him, as Sam always will. The others were "
                "scattered. Boromir died defending Merry and Pippin from orcs. "
                "Aragorn, Legolas, and Gimli have gone after the orcs who captured "
                "the young hobbits. Everything has gone wrong and yet — Frodo is "
                "free of the others, free of the temptation the Ring puts on "
                "mortal hearts. Perhaps this is how it was always meant to be. "
                "Two hobbits walking into Mordor. It sounds absurd. It sounds "
                "exactly right."
            ),
        },
        {
            "date": "2026-03-28",
            "source_type": "photo",
            "text": (
                "No news. The silence stretches on like the road between here and "
                "everywhere that matters. I have been spending my days in the "
                "library, studying old maps of Mordor and the land of Gorgoroth. "
                "Ash plains and poisoned rivers and a great dark mountain belching "
                "fire. My dear Frodo is walking into that. With Sam, yes, loyal "
                "wonderful Sam, but still — two hobbits against all the malice "
                "and darkness of Sauron. I am writing more poetry. A long "
                "narrative piece about the fall of Gondolin that Elrond helped me "
                "with. He was there, in a manner of speaking — he was born in the "
                "aftermath. History is very close in Rivendell."
            ),
        },
        {
            "date": "2026-04-10",
            "source_type": "voice",
            "text": (
                "Something has changed. I cannot explain it but the air feels "
                "different today. Lighter. Elrond felt it too — I saw him on the "
                "balcony at dawn, looking east, and there was something in his "
                "expression I have not seen before. Hope, perhaps. Or relief. "
                "The shadows in the east seemed thinner this morning, and by "
                "afternoon a warm wind came from the south carrying the smell "
                "of growing things. The Elves are singing again, really singing, "
                "not the mournful dirges of the winter months. I dare not hope "
                "too much but my old hobbit heart is beating faster."
            ),
        },
        {
            "date": "2026-04-15",
            "source_type": "photo",
            "text": (
                "The Ring is destroyed. Frodo did it. The news came by eagle — "
                "great eagles out of the north, circling Rivendell and crying "
                "the tidings in voices that shook the valley. The Dark Tower has "
                "fallen. Sauron is no more. Frodo and Sam are alive, found on "
                "the slopes of Mount Doom by Gandalf — Gandalf! He is alive! "
                "Returned, they say, changed, more powerful than before. I wept. "
                "I am not ashamed to say it. I sat in the garden and wept like a "
                "child and every Elf in Rivendell pretended not to notice, which "
                "is the most gracious thing anyone has ever done for me."
            ),
        },
        {
            "date": "2026-04-20",
            "source_type": "photo",
            "text": (
                "I have finished the book. The last chapter — the return home, "
                "the Shire as it was before everything changed. I tied it off "
                "with the words I spoke to Gandalf the night I left: the road "
                "goes ever on and on. It does. It has. And now Frodo's road has "
                "brought him to the end of all things and back again. There and "
                "back again. I shall leave the rest of the pages for him to fill. "
                "His story now, not mine. I am one hundred and twelve years old "
                "and I am tired. But it is a good tired — the kind you feel after "
                "a very long walk when home is finally in sight."
            ),
        },
    ]

    # Seeding does not have access to an embeddings provider (no API keys
    # required to seed), so we force a FixedTokenChunker regardless of the
    # configured strategy. Good enough for populating chunk_count on dev data.
    from journal.services.chunking import FixedTokenChunker
    seed_chunker = FixedTokenChunker(
        max_tokens=config.chunking_max_tokens,
        overlap_tokens=config.chunking_overlap_tokens,
    )

    count = int(args.count) if hasattr(args, "count") and args.count else len(samples)
    created = 0
    for sample in samples[:count]:
        word_count = len(sample["text"].split())
        entry = repo.create_entry(
            sample["date"], sample["source_type"], sample["text"], word_count,
        )
        # Add a page record for photo entries
        if sample["source_type"] == "photo":
            repo.add_entry_page(entry.id, 1, sample["text"])
        # Compute and store chunks (with offsets) so the UI shows the
        # real value and the overlay works even though we don't
        # generate embeddings during seeding.
        chunks = seed_chunker.chunk(sample["text"])
        repo.replace_chunks(entry.id, chunks)
        repo.update_chunk_count(entry.id, len(chunks))
        created += 1
        src = sample["source_type"]
        print(
            f"  Created entry {entry.id}: {sample['date']} "
            f"({src}, {word_count} words, {len(chunks)} chunks)"
        )

    print(f"\nSeeded {created} entries.")
    print("No embeddings generated (re-ingest entries if you want semantic search).")


def cmd_extract_entities(args, config):
    """Run the on-demand entity extraction batch job.

    Accepts a single `--entry-id` to extract one entry, or filter by
    `--start-date`/`--end-date`/`--stale-only` to pick a batch.
    """
    _, _, extraction = _build_services(config)

    if args.entry_id is not None:
        try:
            results = [extraction.extract_from_entry(args.entry_id)]
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        results = extraction.extract_batch(
            start_date=args.start_date,
            end_date=args.end_date,
            stale_only=args.stale_only,
        )

    if not results:
        print("No entries matched the filter — nothing to extract.")
        return

    total_new = sum(r.entities_created for r in results)
    total_matched = sum(r.entities_matched for r in results)
    total_mentions = sum(r.mentions_created for r in results)
    total_rels = sum(r.relationships_created for r in results)
    total_warnings = sum(len(r.warnings) for r in results)

    print(f"Extracted entities for {len(results)} entries:")
    print(f"  Entities created:       {total_new}")
    print(f"  Entities matched:       {total_matched}")
    print(f"  Mentions recorded:      {total_mentions}")
    print(f"  Relationships recorded: {total_rels}")
    print(f"  Warnings:               {total_warnings}")
    if total_warnings:
        print()
        for r in results:
            for w in r.warnings:
                print(f"  [entry {r.entry_id}] {w}")


def cmd_backfill_entity_embeddings(args, config):
    """Re-embed every entity whose description is non-empty.

    The entity's stored embedding (from migration 0004's
    ``embedding_json`` column) feeds stage-c similarity matching during
    entity extraction. Without this command, the embedding is computed
    once at entity creation from name + description and never refreshed
    — so descriptions edited via the webapp after creation don't
    influence future recognition.

    Filters:
    - ``--user-id N`` — restrict to one user.
    - ``--dry-run`` — count candidates without making OpenAI calls.

    Idempotent. Safe to re-run. Cost is small: at
    text-embedding-3-large pricing ($0.13/M tokens, ~50 tokens per
    entity), 500 entities is roughly $0.003.
    """
    conn = get_connection(config.db_path)
    run_migrations(conn)
    entity_store = SQLiteEntityStore(conn)
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    sql = (
        "SELECT id, user_id, canonical_name, description"
        " FROM entities"
        " WHERE description IS NOT NULL"
        " AND TRIM(description) != ''"
    )
    params: list[object] = []
    if args.user_id is not None:
        sql += " AND user_id = ?"
        params.append(args.user_id)
    sql += " ORDER BY id"
    rows = list(conn.execute(sql, params).fetchall())

    scope = (
        f"user {args.user_id}" if args.user_id is not None else "all users"
    )
    print(f"Backfill scope: {scope}")
    print(f"Candidates with non-empty description: {len(rows)}")

    if args.dry_run:
        print("Dry run: no embeddings will be generated.")
        return

    if not rows:
        return

    succeeded = 0
    failed = 0
    for row in rows:
        entity_id = int(row["id"])
        text = f"{row['canonical_name']} {row['description']}".strip()
        try:
            vec = embeddings.embed_query(text)
            entity_store.set_entity_embedding(entity_id, vec)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001 — keep going on per-row error
            failed += 1
            print(f"  ! entity {entity_id}: {exc}", file=sys.stderr)

    print(f"Re-embedded: {succeeded}")
    print(f"Failed:      {failed}")


def cmd_repair_entity_names(args, config):
    """Find and optionally repair entities whose ``canonical_name``
    looks like an LLM-clipped form of a longer token in their mention
    quotes (e.g. ``"Nautilin"`` for a quote ``"Nautiline, ..."``).

    Default is dry-run — pass ``--apply`` to actually update rows.
    Skips proposed repairs that would collide with an existing entity
    of the same canonical_name.
    """
    from journal.providers.extraction import _repair_canonical_name

    conn = get_connection(config.db_path)
    run_migrations(conn)
    entity_store = SQLiteEntityStore(conn)

    # Pull every entity, paginating in case the corpus is large.
    all_entities: list = []
    offset = 0
    page_size = 500
    while True:
        page = entity_store.list_entities(limit=page_size, offset=offset)
        all_entities.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    # Pre-build a lookup for collision detection. Collisions are checked
    # by (user_id, canonical_name) since canonical names are scoped per
    # user.
    by_name: dict[tuple[int, str], int] = {
        (e.user_id, e.canonical_name): e.id for e in all_entities
    }

    repairs: list[tuple[object, str]] = []  # (entity, proposed_name)
    skipped_collisions: list[tuple[object, str]] = []

    for entity in all_entities:
        # First quote that produces a repair wins. Iterate mentions in
        # order so the output is deterministic.
        mentions = entity_store.get_mentions_for_entity(entity.id)
        proposed: str | None = None
        for mention in mentions:
            repaired, was_repaired = _repair_canonical_name(
                entity.canonical_name, mention.quote,
            )
            if was_repaired:
                proposed = repaired
                break
        if proposed is None:
            continue

        if (entity.user_id, proposed) in by_name and by_name[
            (entity.user_id, proposed)
        ] != entity.id:
            skipped_collisions.append((entity, proposed))
            continue
        repairs.append((entity, proposed))

    if not repairs and not skipped_collisions:
        print("No entities need repair.")
        return

    print(f"Proposed repairs ({len(repairs)}):")
    for entity, proposed in repairs:
        print(
            f"  [{entity.id}] {entity.canonical_name!r} -> "
            f"{proposed!r}  (type={entity.entity_type}, "
            f"user_id={entity.user_id})"
        )
    if skipped_collisions:
        print()
        print(
            f"Skipped due to collision with existing entity "
            f"({len(skipped_collisions)}):"
        )
        for entity, proposed in skipped_collisions:
            existing_id = by_name[(entity.user_id, proposed)]
            print(
                f"  [{entity.id}] {entity.canonical_name!r} -> "
                f"{proposed!r} would collide with entity #"
                f"{existing_id}"
            )

    if not args.apply:
        print()
        print("Dry-run only. Pass --apply to make these changes.")
        return

    print()
    print(f"Applying {len(repairs)} repair(s)...")
    applied = 0
    for entity, proposed in repairs:
        try:
            entity_store.update_entity(
                entity.id,
                canonical_name=proposed,
                user_id=entity.user_id,
            )
            applied += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"  Failed to update entity {entity.id}: {exc}",
                file=sys.stderr,
            )
    print(f"Applied {applied}/{len(repairs)} repair(s).")


def cmd_migrate_chromadb(args, config):
    """Add user_id to all ChromaDB vectors for multi-tenant migration."""
    from journal.db.chromadb_migration import backfill_user_id

    updated = backfill_user_id(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
        admin_user_id=1,
    )
    print(f"Updated {updated} ChromaDB documents with user_id=1")


def cmd_stats(args, config):
    """Show journal statistics."""
    _, query, _ = _build_services(config)
    stats = query.get_statistics(args.start_date, args.end_date)

    print("Journal Statistics")
    print(f"  Total entries:          {stats.total_entries}")
    start = stats.date_range_start or "N/A"
    end = stats.date_range_end or "N/A"
    print(f"  Date range:             {start} to {end}")
    print(f"  Total words:            {stats.total_words:,}")
    print(f"  Avg words per entry:    {stats.avg_words_per_entry:.0f}")
    print(f"  Entries per month:      {stats.entries_per_month:.1f}")


def cmd_backfill_mood(args, config):
    """Run the mood-score backfill against the currently-loaded
    dimension set.

    Modes:

    - `--stale-only` (default): score entries missing at least one
      currently-configured dimension. Idempotent.
    - `--force`: rescore every entry in the selected date range,
      regardless of existing state. Use after editing a
      dimension's labels or notes.

    Flags:

    - `--prune-retired`: delete `mood_scores` rows whose dimension
      is not in the current tuple. Off by default. Combined with
      `--dry-run` it reports what would be deleted.
    - `--dry-run`: count what would change without making any
      network or DB writes.
    - `--start-date` / `--end-date`: ISO-8601 window (inclusive).

    The CLI prints an estimated cost using public Sonnet-4.5
    pricing so the user can decide whether to proceed on a large
    corpus.
    """
    from journal.providers.mood_scorer import AnthropicMoodScorer
    from journal.services.backfill import backfill_mood_scores
    from journal.services.mood_dimensions import load_mood_dimensions
    from journal.services.mood_scoring import MoodScoringService

    try:
        dimensions = load_mood_dimensions(config.mood_dimensions_path)
    except Exception as e:
        print(
            f"Error: failed to load mood dimensions: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    scorer = AnthropicMoodScorer(
        api_key=config.anthropic_api_key,
        model=config.mood_scorer_model,
        max_tokens=config.mood_scorer_max_tokens,
    )
    service = MoodScoringService(scorer, repo, dimensions)

    mode = "force" if args.force else "stale-only"
    print(f"Mood backfill — mode={mode}, dimensions={len(dimensions)}")
    for d in dimensions:
        print(f"  - {d.name} ({d.scale_type})")
    if args.dry_run:
        print("Dry run: no scoring or writes will occur.")

    result = backfill_mood_scores(
        repository=repo,
        mood_scoring=service,
        mode=mode,
        start_date=args.start_date,
        end_date=args.end_date,
        prune_retired=args.prune_retired,
        dry_run=args.dry_run,
    )

    prefix = "[dry-run] " if result.dry_run else ""
    print(f"{prefix}Scored:          {result.scored}")
    print(f"{prefix}Skipped:         {result.skipped}")
    if args.prune_retired:
        print(f"{prefix}Pruned retired:  {result.pruned}")
    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  {err}")

    # Rough cost estimate using public Sonnet 4.5 pricing: $3/M
    # input tokens, $15/M output. Per-entry call is ~1250 input
    # tokens (prompt ~500 + ~750 for a 500-word entry) + ~150
    # output tokens. Adjust if you change the model.
    if result.scored and not result.dry_run:
        input_cost = result.scored * 1250 * 3.0 / 1_000_000
        output_cost = result.scored * 150 * 15.0 / 1_000_000
        total = input_cost + output_cost
        print(f"\nEstimated cost for this run: ${total:.4f}")


def cmd_health(args, config):
    """Print the same payload served by the `/health` HTTP endpoint.

    Builds the services locally, runs the ingestion stats query
    and all liveness checks, and emits the result as pretty JSON
    (default) or a compact single-line JSON blob (`--compact`).

    Exit code is 0 when the overall status is `ok` or `degraded`,
    non-zero when it is `error`. Docker / cron consumers can pipe
    the output to `jq` or `grep` without caring about the format.
    """
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime

    from journal.services.liveness import (
        check_api_key,
        check_chromadb,
        check_sqlite,
        overall_status,
    )

    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)
    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )

    ingestion = repo.get_ingestion_stats(now=datetime.now(UTC))
    checks = [
        check_sqlite(conn),
        check_chromadb(vector_store),
        check_api_key("anthropic", config.anthropic_api_key),
        check_api_key("openai", config.openai_api_key),
    ]
    status = overall_status(checks)

    payload = {
        "status": status,
        "checks": [asdict(c) for c in checks],
        "ingestion": asdict(ingestion),
        # The CLI builds its services fresh, so there are no query
        # stats to show — the in-process stats collector is only
        # populated by the long-running server. Surface an explicit
        # zero rather than pretending.
        "queries": {
            "total_queries": 0,
            "uptime_seconds": 0.0,
            "started_at": None,
            "by_type": {},
        },
    }

    if args.compact:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))

    if status == "error":
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(
        prog="journal",
        description="Journal Analysis Tool — ingest and query personal journal entries",
    )
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ingest
    p_ingest = subparsers.add_parser("ingest", help="Ingest a journal entry from image or audio")
    p_ingest.add_argument("file", help="Path to image or audio file")
    p_ingest.add_argument("--date", help="Entry date (ISO 8601, default: today)")
    p_ingest.add_argument("--language", default="en", help="Language for voice transcription")

    # ingest-multi
    p_ingest_multi = subparsers.add_parser(
        "ingest-multi", help="Ingest multiple pages as one entry"
    )
    p_ingest_multi.add_argument("files", nargs="+", help="Paths to image files (in page order)")
    p_ingest_multi.add_argument("--date", help="Entry date (ISO 8601, default: today)")

    # search
    p_search = subparsers.add_parser("search", help="Search entries semantically")
    p_search.add_argument("query", help="Natural language search query")
    p_search.add_argument("--start-date", help="Filter from date")
    p_search.add_argument("--end-date", help="Filter until date")
    p_search.add_argument("--limit", type=int, default=10, help="Max results")

    # list
    p_list = subparsers.add_parser("list", help="List entries")
    p_list.add_argument("--start-date", help="Filter from date")
    p_list.add_argument("--end-date", help="Filter until date")
    p_list.add_argument("--limit", type=int, default=20, help="Max results")

    # stats
    p_stats = subparsers.add_parser("stats", help="Show statistics")
    p_stats.add_argument("--start-date", help="Filter from date")
    p_stats.add_argument("--end-date", help="Filter until date")

    # health
    p_health = subparsers.add_parser(
        "health",
        help="Print the operational health payload (same shape as GET /health)",
    )
    p_health.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact single-line JSON instead of the default indented form",
    )

    # backfill-chunks
    subparsers.add_parser(
        "backfill-chunks",
        help="Re-run the chunker and update stored chunk_count (no re-embedding)",
    )

    # rechunk
    p_rechunk = subparsers.add_parser(
        "rechunk",
        help="Re-chunk and re-embed every entry using the current strategy",
    )
    p_rechunk.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to ChromaDB or SQLite",
    )

    # backfill-mood
    p_backfill_mood = subparsers.add_parser(
        "backfill-mood",
        help=(
            "Score journal entries against the configured mood "
            "dimensions (sparse by default — only entries missing "
            "a current dimension unless --force)"
        ),
    )
    p_backfill_mood.add_argument(
        "--force",
        action="store_true",
        help=(
            "Rescore every entry in the window, not just those "
            "missing a current dimension"
        ),
    )
    p_backfill_mood.add_argument(
        "--prune-retired",
        action="store_true",
        help=(
            "Delete mood_scores rows whose dimension is not in "
            "the current config. Off by default; historical "
            "scores are preserved unless you pass this flag."
        ),
    )
    p_backfill_mood.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Count what would be scored/pruned without making "
            "any network or DB writes"
        ),
    )
    p_backfill_mood.add_argument(
        "--start-date",
        help="Filter entries from this date (inclusive, ISO 8601)",
    )
    p_backfill_mood.add_argument(
        "--end-date",
        help="Filter entries until this date (inclusive, ISO 8601)",
    )

    # eval-chunking
    p_eval = subparsers.add_parser(
        "eval-chunking",
        help="Measure chunking quality (cohesion / separation / ratio)",
    )
    p_eval.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output",
    )

    # seed
    p_seed = subparsers.add_parser(
        "seed", help="Seed database with sample entries (no API keys needed)",
    )
    p_seed.add_argument("--count", type=int, help="Number of sample entries (default: all 5)")

    # migrate-chromadb
    subparsers.add_parser(
        "migrate-chromadb",
        help="Add user_id metadata to all ChromaDB vectors (multi-tenant migration)",
    )

    # extract-entities
    p_extract = subparsers.add_parser(
        "extract-entities",
        help="Run the entity extraction batch job over one or more entries",
    )
    p_extract.add_argument("--entry-id", type=int, help="Extract a single entry by id")
    p_extract.add_argument("--start-date", help="Filter entries from this date (ISO 8601)")
    p_extract.add_argument("--end-date", help="Filter entries until this date (ISO 8601)")
    p_extract.add_argument(
        "--stale-only",
        action="store_true",
        help="Only process entries flagged as stale",
    )

    # backfill-entity-embeddings
    p_reembed = subparsers.add_parser(
        "backfill-entity-embeddings",
        help=(
            "Re-embed every entity that has a non-empty description so "
            "the stored embedding reflects the current text. Used after "
            "deploying the description-driven recognition feature."
        ),
    )
    p_reembed.add_argument(
        "--user-id",
        type=int,
        help="Restrict the backfill to one user (default: all users)",
    )
    p_reembed.add_argument(
        "--dry-run",
        action="store_true",
        help="Count candidates without calling the embeddings API",
    )

    # repair-entity-names
    p_repair = subparsers.add_parser(
        "repair-entity-names",
        help=(
            "Find entities whose canonical_name was clipped by the LLM "
            "(e.g. 'Nautilin' instead of 'Nautiline'). Dry-run by default; "
            "pass --apply to update rows."
        ),
    )
    p_repair.add_argument(
        "--apply",
        action="store_true",
        help="Apply proposed repairs (default is dry-run)",
    )

    args = parser.parse_args()
    setup_logging(args.log_level)
    config = load_config()

    commands = {
        "ingest": cmd_ingest,
        "ingest-multi": cmd_ingest_multi,
        "search": cmd_search,
        "list": cmd_list,
        "stats": cmd_stats,
        "health": cmd_health,
        "backfill-chunks": cmd_backfill_chunks,
        "backfill-mood": cmd_backfill_mood,
        "rechunk": cmd_rechunk,
        "eval-chunking": cmd_eval_chunking,
        "seed": cmd_seed,
        "extract-entities": cmd_extract_entities,
        "backfill-entity-embeddings": cmd_backfill_entity_embeddings,
        "repair-entity-names": cmd_repair_entity_names,
        "migrate-chromadb": cmd_migrate_chromadb,
    }
    commands[args.command](args, config)
