transcript_path = "spike/transcript_copy.jsonl"
backlog_dir = "/Users/mhm/Documents/Dev/centauri-sentinel/docs/backlog"


def extract_prompt() -> None:
    with open(transcript_path) as f:
        for line in f:
            if 'step_index":84' in line or 'step_index": 84' in line:
                prompt_key = '\\"Prompt\\":\\"'
                idx = line.find(prompt_key)
                if idx != -1:
                    start = idx + len(prompt_key)
                    print("Start of prompt content:", line[start : start + 200])
                    # Print characters from index 1000 onwards to find where the Prompt ends
                    print("Snippet near index 11000:", line[11000:11500])
                    # Let's search for "Role"
                    role_idx = line.find("Role", start)
                    if role_idx != -1:
                        print("Found 'Role' at:", role_idx)
                        print("Context around 'Role':", line[role_idx - 100 : role_idx + 50])
                    else:
                        print("Could not find 'Role'")
                else:
                    print("Could not find prompt key")
    return None


def main() -> None:
    prompt = extract_prompt()
    if not prompt:
        print("Could not find prompt for step_index 84")
        return

    print("Found prompt! Length:", len(prompt))
    # Write the prompt to a temp file to inspect
    with open("spike/extracted_prompt.txt", "w") as f:
        f.write(prompt)
    print("Wrote prompt to spike/extracted_prompt.txt")


if __name__ == "__main__":
    main()
