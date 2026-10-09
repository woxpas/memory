## Woxpas — Identity Export Prompt

Use this prompt against ChatGPT, Claude, Gemini, or any AI you've used long
enough that it has accumulated context about you. Paste it as your message,
save the response as a `.md` file, then import it with:

    woxpas-cli init-tenant --display-name "<your name>" --identity-from <saved.md>

Or `woxpas-cli identity-template` to print this prompt to stdout.

---

Please summarize everything you know about me from our conversations and your
stored memory of me, in the markdown format below. Be concise and factual.
Use bullet points. Leave a section empty if you have no information for it.

# Identity

## Bio
- Name, location, pronouns, basic personal facts

## Profession
- Role, industry, current employer, career trajectory

## Communication style
- How I like to be communicated with — tone, formality, what I dislike,
  language preferences, anything you've adapted to over time

## Projects
- Active initiatives I'm working on, with brief descriptions and current state

## Preferences
- Tools I prefer, working hours, dietary, lifestyle choices, interests

## Expertise
- Areas where I have deep knowledge or am authoritative

## Relationships
- (Optional) Important people in my life — family, close colleagues — only
  if context is relevant to how you advise me

## Goals
- (Optional) Long-term goals, aspirations, or what I'm working toward

## Health
- (Optional, sensitive) Health-related preferences or conditions only if
  they affect how you should advise me. Leave empty if unsure.

## Other
- Anything noteworthy that doesn't fit above

---

Format conventions:
- Each bullet should be standalone (it'll be parsed as one fact)
- Prefer "Key: Value" format when natural ("Location: Berlin, Germany")
- One concept per bullet; split compound facts into multiple bullets
- Skip a section entirely (leave empty) if you have no relevant info
