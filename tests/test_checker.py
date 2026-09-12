import asyncio
import checker, tone, summary

def test_mats_preserved():
    # маты должны остаться, как дила -> как дела
    async def run():
        out = await checker.proofread("привет как дила всо харашо блять")
        assert "блять" in out.lower(), f"мат потерян: {out}"
        assert "как дела" in out.lower(), f"не исправил: {out}"
        out2 = await checker.proofread("я пашёл нахуй отсюда кек")
        assert "нахуй" in out2.lower()
        # privet -> привет
        out3 = await checker.proofread("privet kak dela vsyo normalno")
        assert "привет" in out3.lower() or "Привет" in out3
        print("checker mats OK")
        # tone
        out4 = await tone.annotate_tone("Привет, как дела? Всё отлично!")
        assert "[" in out4 and "]" in out4
        print("tone OK")
        # summary
        long = "Привет, как дела? " * 200  # >800
        out5 = await summary.summarize(long)
        assert len(out5) < len(long) and len(out5) > 10
        print("summary OK")
        # short should return ""
        out6 = await summary.summarize("коротко")
        assert out6 == ""
        print("summary short OK")
        print("ALL TESTS PASS")
    asyncio.run(run())

if __name__ == "__main__":
    test_mats_preserved()
