import json

subagent_transcript_path = "spike/subagent_transcript.jsonl"


def main() -> None:
    with open(subagent_transcript_path) as f:
        first_line = f.readline()

    try:
        data = json.loads(first_line, strict=False)
        content = data.get("content", "")
        print("Prompt length:", len(content))
        with open("spike/full_subagent_prompt.txt", "w") as f:
            f.write(content)
        print("Wrote prompt to spike/full_subagent_prompt.txt")
    except Exception as e:
        print("Error parsing first line:", e)


if __name__ == "__main__":
    main()
