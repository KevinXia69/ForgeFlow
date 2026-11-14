/**
 * Compatibility grammar copied from @deepseek-ai/dsh-client-ui-primitives
 * 0.2.1-rc.2, lib/types/markdown/mathCompatibility.js (distributed lib/index.js).
 * Upstream: https://github.com/deepseek-ai/deepseek-harness/tree/main/packages/client/ui-primitives
 * Keep this grammar aligned with the locked Markdown renderer when upgrading;
 * privacy parsing must recognize exactly the same TeX boundaries.
 * Changes: TypeScript annotations and module imports only.
 *
 * MIT License
 *
 * Copyright (c) 2026 DeepSeek
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
import { markdownLineEnding } from "micromark-util-character";
import { codes, constants, types } from "micromark-util-symbol";
import { factorySpace } from "micromark-factory-space";
import type { Code, Construct, Effects, Extension, State, Tokenizer } from "micromark-util-types";

/** Extend upstream dollar-only math syntax with TeX delimiters while reusing its token vocabulary. */
const previousBackslash: NonNullable<Construct["previous"]> = function(code) {
  if (code !== codes.backslash) return true;
  const tail = this.events.at(-1);
  /* v8 ignore next -- a previous code necessarily has a preceding event. */
  if (tail === void 0) return false;
  return tail[1].type === types.characterEscape;
};
const tokenizeBackslashMathText: Tokenizer = function(effects, ok, nok) {
  return start;
  function start(code: Code): State | undefined {
    /* v8 ignore next -- the text construct is dispatched only for a backslash. */
    if (code !== codes.backslash) return nok(code);
    effects.enter("mathText");
    effects.enter("mathTextSequence");
    effects.consume(code);
    return open;
  }
  function open(code: Code): State | undefined {
    if (code !== codes.leftParenthesis) return nok(code);
    effects.consume(code);
    effects.exit("mathTextSequence");
    return between;
  }
  function between(code: Code): State | undefined {
    if (code === codes.eof) return nok(code);
    if (code === codes.backslash) return effects.attempt({
      partial: true,
      tokenize: tokenizeClose
    }, close, afterCloseAttempt)(code);
    if (markdownLineEnding(code)) {
      effects.enter(types.lineEnding);
      effects.consume(code);
      effects.exit(types.lineEnding);
      return between;
    }
    return dataStart(code);
  }
  function afterCloseAttempt(code: Code): State | undefined {
    return effects.check({
      partial: true,
      tokenize: tokenizeOpen
    }, nok, dataStart)(code);
  }
  function dataStart(code: Code): State | undefined {
    effects.enter("mathTextData");
    effects.consume(code);
    return code === codes.backslash ? afterDataBackslash : data;
  }
  function afterDataBackslash(code: Code): State | undefined {
    if (code === codes.backslash) {
      effects.consume(code);
      return data;
    }
    return data(code);
  }
  function data(code: Code): State | undefined {
    if (code === codes.eof || code === codes.backslash || markdownLineEnding(code)) {
      effects.exit("mathTextData");
      return between(code);
    }
    effects.consume(code);
    return data;
  }
  function close(code: Code): State | undefined {
    effects.exit("mathText");
    return ok(code);
  }
  function tokenizeClose(closeEffects: Effects, closeOk: State, closeNok: State): State {
    return slash;
    function slash(code: Code): State | undefined {
      /* v8 ignore next -- this partial construct is attempted only at a backslash. */
      if (code !== codes.backslash) return closeNok(code);
      closeEffects.enter("mathTextSequence");
      closeEffects.consume(code);
      return parenthesis;
    }
    function parenthesis(code: Code): State | undefined {
      if (code !== codes.rightParenthesis) return closeNok(code);
      closeEffects.consume(code);
      closeEffects.exit("mathTextSequence");
      return closeOk;
    }
  }
  function tokenizeOpen(openEffects: Effects, openOk: State, openNok: State): State {
    return slash;
    function slash(code: Code): State | undefined {
      /* v8 ignore next -- the opening check follows a failed close attempt at a backslash. */
      if (code !== codes.backslash) return openNok(code);
      openEffects.enter(types.chunkString);
      openEffects.consume(code);
      return parenthesis;
    }
    function parenthesis(code: Code): State | undefined {
      if (code !== codes.leftParenthesis) return openNok(code);
      openEffects.consume(code);
      openEffects.exit(types.chunkString);
      return openOk;
    }
  }
};
function createMathFlow(marker: Code, openMarker: Code, closeMarker: Code, multiline: boolean): Construct {
  const tokenize: Tokenizer = function(effects, ok, nok) {
    const self = this;
    let oddBackslashRun = false;
    const tail = self.events.at(-1);
    const initialSize = tail?.[1].type === types.linePrefix ? tail[2].sliceSerialize(tail[1], true).length : 0;
    return start;
    function start(code: Code): State | undefined {
      /* v8 ignore next -- the flow construct is dispatched only for its marker. */
      if (code !== marker) return nok(code);
      effects.enter("mathFlow");
      effects.enter("mathFlowFence");
      effects.enter("mathFlowFenceSequence");
      effects.consume(code);
      return open;
    }
    function open(code: Code): State | undefined {
      if (code !== openMarker) return nok(code);
      effects.consume(code);
      effects.exit("mathFlowFenceSequence");
      effects.exit("mathFlowFence");
      return marker === codes.dollarSign ? afterDollarOpen : content;
    }
    function afterDollarOpen(code: Code): State | undefined {
      return code === codes.dollarSign ? nok(code) : content(code);
    }
    function content(code: Code): State | undefined {
      if (code === codes.eof) return nok(code);
      if (code === marker && (marker !== codes.dollarSign || !oddBackslashRun)) return effects.attempt({
        partial: true,
        tokenize: tokenizeClosingFence
      }, closed, afterClosingFenceAttempt)(code);
      if (markdownLineEnding(code)) return multiline ? effects.attempt(nonLazyContinuation, afterContinuation, nok)(code) : nok(code);
      return valueStart(code);
    }
    function afterClosingFenceAttempt(code: Code): State | undefined {
      return marker === codes.backslash ? effects.check({
        partial: true,
        tokenize: tokenizeOpeningFence
      }, nok, markerValueStart)(code) : markerValueStart(code);
    }
    function afterContinuation(code: Code): State | undefined {
      return effects.attempt({
        partial: true,
        tokenize: tokenizeClosingFence
      }, closed, initialSize ? factorySpace(effects, content, types.linePrefix, initialSize + 1) : content)(code);
    }
    function valueStart(code: Code): State | undefined {
      effects.enter("mathFlowValue");
      oddBackslashRun = code === codes.backslash;
      effects.consume(code);
      return value;
    }
    function markerValueStart(code: Code): State | undefined {
      effects.enter("mathFlowValue");
      oddBackslashRun = false;
      effects.consume(code);
      return valueAfterMarker;
    }
    function valueAfterMarker(code: Code): State | undefined {
      if (code === marker) {
        effects.consume(code);
        return value;
      }
      return value(code);
    }
    function value(code: Code): State | undefined {
      if (code === codes.eof || code === marker || markdownLineEnding(code)) {
        effects.exit("mathFlowValue");
        return content(code);
      }
      oddBackslashRun = code === codes.backslash ? !oddBackslashRun : false;
      effects.consume(code);
      return value;
    }
    function closed(code: Code): State | undefined {
      effects.exit("mathFlow");
      return ok(code);
    }
    function tokenizeClosingFence(closeEffects: Effects, closeOk: State, closeNok: State): State {
      return factorySpace(closeEffects, sequenceStart, types.linePrefix, constants.tabSize);
      function sequenceStart(code: Code): State | undefined {
        if (code !== marker) return closeNok(code);
        closeEffects.enter("mathFlowFence");
        closeEffects.enter("mathFlowFenceSequence");
        closeEffects.consume(code);
        return sequenceEnd;
      }
      function sequenceEnd(code: Code): State | undefined {
        if (code !== closeMarker) return closeNok(code);
        closeEffects.consume(code);
        closeEffects.exit("mathFlowFenceSequence");
        return factorySpace(closeEffects, after, types.whitespace);
      }
      function after(code: Code): State | undefined {
        if (code !== codes.eof && !markdownLineEnding(code)) return closeNok(code);
        closeEffects.exit("mathFlowFence");
        return closeOk(code);
      }
    }
    function tokenizeOpeningFence(openEffects: Effects, openOk: State, openNok: State): State {
      return sequenceStart;
      function sequenceStart(code: Code): State | undefined {
        /* v8 ignore next -- the opening check follows a failed close attempt at the marker. */
        if (code !== marker) return openNok(code);
        openEffects.enter(types.chunkString);
        openEffects.consume(code);
        return sequenceEnd;
      }
      function sequenceEnd(code: Code): State | undefined {
        if (code !== openMarker) return openNok(code);
        openEffects.consume(code);
        openEffects.exit(types.chunkString);
        return openOk;
      }
    }
  };
  return {
    concrete: true,
    name: marker === codes.dollarSign ? "sameLineDollarMathFlow" : "backslashMathFlow",
    tokenize
  };
}
const tokenizeNonLazyContinuation: Tokenizer = function(effects, ok, nok) {
  const self = this;
  return start;
  function start(code: Code): State | undefined {
    /* v8 ignore next -- continuation constructs are attempted only after a line ending. */
    if (code === codes.eof) return ok(code);
    /* v8 ignore next -- continuation constructs are attempted only after a line ending. */
    if (!markdownLineEnding(code)) return nok(code);
    effects.enter(types.lineEnding);
    effects.consume(code);
    effects.exit(types.lineEnding);
    return lineStart;
  }
  function lineStart(code: Code): State | undefined {
    return self.parser.lazy[self.now().line] ? nok(code) : ok(code);
  }
};
const nonLazyContinuation: Construct = {
  partial: true,
  tokenize: tokenizeNonLazyContinuation
};
const backslashMathText: Construct = {
  name: "backslashMathText",
  previous: previousBackslash,
  tokenize: tokenizeBackslashMathText
};
const backslashMathFlow = createMathFlow(codes.backslash, codes.leftSquareBracket, codes.rightSquareBracket, true);
const sameLineDollarMathFlow = createMathFlow(codes.dollarSign, codes.dollarSign, codes.dollarSign, false);
const backslashMath: Extension = {
  flow: {
    [codes.backslash]: backslashMathFlow,
    [codes.dollarSign]: sameLineDollarMathFlow
  },
  text: { [codes.backslash]: backslashMathText }
};
/**
* TeX backslash delimiters and same-line display-dollar blocks as a micromark
* syntax extension reusing `micromark-extension-math`'s token vocabulary; the
* caller must also register `math()` on the same parse so the emitted tokens
* compile to standard math nodes.
* @returns The micromark syntax extension.
*/
export function mathCompatibility(): Extension {
  return backslashMath;
}
