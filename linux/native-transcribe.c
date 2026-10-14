#define _POSIX_C_SOURCE 200809L
#include "transcribe.h"
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/prctl.h>
#include <openssl/evp.h>

static int model_integrity(const char *path) {
  static const char expected[]="cf83cedc6777d2528a4d2a8a4bf476174ef7e5c65b86bc6c955f4cc9f5951ac5";
  FILE *f=fopen(path,"rb");
  if(!f) return 0;
  struct stat st;
  if(fstat(fileno(f),&st) || !S_ISREG(st.st_mode) || st.st_size!=873443456) { fclose(f); return 0; }
  EVP_MD_CTX *ctx=EVP_MD_CTX_new();
  unsigned char buffer[65536],digest[32]; unsigned int length=0;
  int ok=ctx && EVP_DigestInit_ex(ctx,EVP_sha256(),NULL)==1;
  size_t n;
  while(ok && (n=fread(buffer,1,sizeof(buffer),f))) ok=EVP_DigestUpdate(ctx,buffer,n)==1;
  ok=ok && !ferror(f) && EVP_DigestFinal_ex(ctx,digest,&length)==1 && length==32;
  EVP_MD_CTX_free(ctx); fclose(f);
  char hex[65];
  if(ok) { for(unsigned int i=0;i<32;i++) snprintf(hex+2*i,3,"%02x",digest[i]); ok=!strcmp(hex,expected); }
  return ok;
}
extern void openblas_set_num_threads(int) __attribute__((weak));
static int configure_threads(int threads) {
  char number[16]; snprintf(number,sizeof(number),"%d",threads);
  if(setenv("OPENBLAS_NUM_THREADS",number,1) || setenv("OMP_NUM_THREADS",number,1) || setenv("MKL_NUM_THREADS",number,1)) return 0;
  /* The optional BLAS decoder uses a separate CPU thread pool. */
  if(openblas_set_num_threads) openblas_set_num_threads(threads);
  return 1;
}
static volatile sig_atomic_t cancelled;
static void stop(int signal_number) { (void)signal_number; cancelled = 1; }
static bool abort_run(void *data) { (void)data; return cancelled != 0; }
static int fail(const char *message) { fprintf(stderr, "parakeet-transcribe: %s\n", message); return 1; }
static uint32_t le32(const unsigned char *p) { return p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24; }
static uint16_t le16(const unsigned char *p) { return p[0] | (uint16_t)p[1]<<8; }
static float *read_audio(const char *path, int *count) {
  FILE *f = fopen(path, "rb");
  if (!f) return NULL;
  unsigned char h[12], c[8], fmt[16];
  float *pcm = NULL;
  int valid = 0;
  struct stat st;
  if (fstat(fileno(f), &st) || !S_ISREG(st.st_mode) || fread(h,1,12,f)!=12 || memcmp(h,"RIFF",4) || memcmp(h+8,"WAVE",4) || (uint64_t)le32(h+4)+8 != (uint64_t)st.st_size) goto done;
  size_t chunk_read;
  while ((chunk_read=fread(c,1,8,f))==8) {
    uint32_t size = le32(c+4);
    long pos = ftell(f);
    if (pos < 0 || (uint64_t)pos + size + (size & 1) > (uint64_t)st.st_size) goto done;
    if (!memcmp(c,"fmt ",4)) {
      if (size < 16 || fread(fmt,1,16,f)!=16) goto done;
      valid = le16(fmt)==1 && le16(fmt+2)==1 && le32(fmt+4)==16000 && le32(fmt+8)==32000 && le16(fmt+12)==2 && le16(fmt+14)==16;
    } else if (!memcmp(c,"data",4)) {
      if (!valid || !size || size%2 || size/2 > INT_MAX || pcm) goto done;
      *count = (int)(size/2);
      pcm = malloc((size_t)*count*sizeof(float));
      if (!pcm) goto done;
      unsigned char block[8192];
      int offset = 0;
      while (offset < *count) {
        int n = *count-offset;
        if (n > 4096) n=4096;
        if (fread(block,2,(size_t)n,f)!=(size_t)n) goto done;
        for(int i=0;i<n;i++) pcm[offset+i]=(int16_t)le16(block+2*i)/32768.0f;
        offset+=n;
      }
    }
    if (fseek(f,pos+(long)size+(size&1),SEEK_SET)) goto done;
  }
  if (pcm && valid && !chunk_read && !ferror(f) && ftell(f)==st.st_size) { fclose(f); return pcm; }
 done:
  free(pcm); fclose(f); return NULL;
}
static void json_string(FILE *out, const char *text) {
  fputc('"',out);
  for (const unsigned char *p=(const unsigned char *)text; *p;p++) {
    if (*p=='"' || *p=='\\') { fputc('\\',out); fputc(*p,out); }
    else if (*p<32) fprintf(out,"\\u%04x",*p);
    else fputc(*p,out);
  }
  fputc('"',out);
}
static size_t characters(const char *s, size_t bytes) {
  size_t n=0;
  for(size_t i=0;i<bytes;i++) if (((unsigned char)s[i]&0xc0)!=0x80) n++;
  return n;
}
static int space(unsigned char c) { return c==' ' || c=='\t' || c=='\r' || c=='\n'; }
static double bounded(int64_t ms, double duration) { double t=ms/1000.0; return t<0?0:t>duration?duration:t; }
static int write_result(FILE *out, struct transcribe_session *session, struct transcribe_model *model, double duration, const char *backend) {
  fprintf(out,"{\"schemaVersion\":1,\"engine\":\"multitalker-parakeet\",\"speakerCapacity\":4,\"durationSeconds\":%.6f,\"segments\":[",duration);
  int emitted=0;
  for(int i=0;i<transcribe_n_segments(session);i++) {
    struct transcribe_segment s; transcribe_segment_init(&s);
    if(transcribe_get_segment(session,i,&s)!=TRANSCRIBE_OK || !s.text || s.speaker_id<0 || s.speaker_id>4) return 0;
    if(!*s.text) continue;
    if(emitted++) fputc(',',out);
    fprintf(out,"{\"startTime\":%.6f,\"endTime\":%.6f,\"text\":",bounded(s.t0_ms,duration),bounded(s.t1_ms,duration)); json_string(out,s.text);
    fprintf(out,",\"speakerTag\":"); if(s.speaker_id) fprintf(out,"%d",s.speaker_id); else fputs("null",out);
    /* Only publish decoder words whose exact spans and timing are available. */
    size_t cursor=0, char_cursor=0;
    int words=0;
    for(int j=0;j<s.n_words;j++) {
      struct transcribe_word w; transcribe_word_init(&w);
      if(transcribe_get_word(session,s.first_word+j,&w)!=TRANSCRIBE_OK || !w.text) return 0;
      while(space((unsigned char)s.text[cursor])) { cursor++; char_cursor++; }
      const char *text=w.text; while(space((unsigned char)*text)) text++;
      size_t length=strlen(text); while(length && space((unsigned char)text[length-1])) length--;
      if(!length || strncmp(s.text+cursor,text,length) || (s.text[cursor+length] && !space((unsigned char)s.text[cursor+length]))) break;
      size_t start=cursor, start_char=char_cursor; cursor+=length; char_cursor+=characters(s.text+start,length);
      if(w.t0_ms<0 || w.t1_ms<=w.t0_ms || w.t0_ms/1000.0>=duration) continue;
      if(!words++) fputs(",\"words\":[",out); else fputc(',',out);
      fprintf(out,"{\"text\":");
      /* Span text comes directly from the segment, including punctuation. */
      fputc('"',out);
      for(size_t k=start;k<cursor;k++) { unsigned char c=s.text[k]; if(c=='"'||c=='\\') fputc('\\',out); if(c<32) fprintf(out,"\\u%04x",c); else fputc(c,out); }
      fprintf(out,"\",\"startTime\":%.6f,\"endTime\":%.6f,\"startChar\":%zu,\"endChar\":%zu}",bounded(w.t0_ms,duration),bounded(w.t1_ms,duration),start_char,char_cursor);
    }
    if(words) fputc(']',out);
    fputc('}',out);
  }
  fputs("],\"speakerTurns\":[",out);
  emitted=0;
  for(int i=0;i<transcribe_n_speaker_segments(session);i++) {
    struct transcribe_speaker_segment s; transcribe_speaker_segment_init(&s);
    if(transcribe_get_speaker_segment(session,i,&s)!=TRANSCRIBE_OK || s.speaker_id<1 || s.speaker_id>4) return 0;
    if(s.t0_ms<0 || s.t1_ms<=s.t0_ms || s.t0_ms/1000.0>=duration) continue;
    if(emitted++) fputc(',',out);
    fprintf(out,"{\"startTime\":%.6f,\"endTime\":%.6f,\"speakerTag\":%d}",bounded(s.t0_ms,duration),bounded(s.t1_ms,duration),s.speaker_id);
  }
  fputs("],\"provenance\":{\"runtimeVersion\":",out); json_string(out,transcribe_version());
  fputs(",\"runtimeCommit\":",out); json_string(out,transcribe_version_commit());
  fputs(",\"modelRevision\":\"a9a7208d8f205b5816770a6f7fb83afc81a7691b\",\"timingOrigin\":\"decoder-alignment-milliseconds\",\"speakerOrigin\":\"bundled-sortformer-4spk-v2.1\",\"mode\":\"per-speaker-streaming-kernel\",\"backend\":",out); json_string(out,backend);
  fputs(",\"backendDevice\":",out); json_string(out,transcribe_model_backend(model)); fputs("}}\n",out);
  return !ferror(out);
}
static int run_request(struct transcribe_model *model, const char *model_path, const char *audio, const char *output, int threads, const char *backend) {
  struct stat destination, input;
  if(!*audio || !*output) return fail("Audio and output paths must not be empty.");
  if(!stat(output,&destination) && ((!stat(audio,&input) && destination.st_dev==input.st_dev && destination.st_ino==input.st_ino) || (!stat(model_path,&input) && destination.st_dev==input.st_dev && destination.st_ino==input.st_ino))) return fail("Output must not overwrite the input audio or model.");
  int count=0; float *pcm=read_audio(audio,&count);
  if(!pcm) return fail("Audio must be a nonempty regular RIFF WAV, 16 kHz mono PCM16, with complete data.");
  struct transcribe_session *session=NULL;
  int result=1;
  struct transcribe_session_params options; transcribe_session_params_init(&options); options.n_threads=threads;
  if(transcribe_session_init(model,&options,&session)!=TRANSCRIBE_OK) { fail("Could not initialize native inference session."); goto done; }
  transcribe_set_abort_callback(session,abort_run,NULL);
  struct transcribe_run_params params; transcribe_run_params_init(&params); params.language="en"; params.timestamps=TRANSCRIBE_TIMESTAMPS_WORD; params.diarize=TRANSCRIBE_DIARIZE_MODE_ON;
  transcribe_status status=cancelled?TRANSCRIBE_ERR_ABORTED:transcribe_run(session,pcm,count,&params);
  if(status!=TRANSCRIBE_OK || cancelled || transcribe_was_truncated(session)) { fprintf(stderr,"Inference did not complete (status %d); no output published.\n",status); goto done; }
  size_t size=strlen(output)+16; char *temporary=malloc(size);
  if(!temporary) { fail("Output path allocation failed."); goto done; }
  snprintf(temporary,size,"%s.tmp.XXXXXX",output);
  int fd=mkstemp(temporary);
  FILE *out=fd<0?NULL:fdopen(fd,"w");
  if(!out) { if(fd>=0) { close(fd); unlink(temporary); } fail("Cannot create private output beside requested JSON path."); free(temporary); goto done; }
  int written=write_result(out,session,model,count/16000.0,backend);
  if(fflush(out) || fsync(fileno(out))) written=0;
  if(fclose(out)) written=0;
  if(!written || cancelled || rename(temporary,output)) { unlink(temporary); fail("Cannot publish complete valid output."); } else result=0;
  free(temporary);
 done:
  transcribe_session_free(session); free(pcm); return result;
}
static int worker_loop(FILE *protocol, struct transcribe_model *model, const char *model_path, int threads, const char *backend) {
  char *audio=NULL,*output=NULL; size_t audio_size=0,output_size=0;
  int result=0;
  if(fputs("ready\n",protocol)==EOF || fflush(protocol)) return fail("Cannot announce worker readiness.");
  while(!cancelled) {
    ssize_t n=getdelim(&audio,&audio_size,'\0',stdin);
    if(n<0) { if(!cancelled && !feof(stdin)) result=fail("Cannot read worker request."); break; }
    if(audio[n-1]!='\0') { result=fail("Incomplete worker audio path."); break; }
    n=getdelim(&output,&output_size,'\0',stdin);
    if(n<0 || output[n-1]!='\0') { result=fail("Incomplete worker output path."); break; }
    int request_result=run_request(model,model_path,audio,output,threads,backend);
    if(fputs(request_result?"error\n":"ok\n",protocol)==EOF || fflush(protocol)) { result=fail("Cannot acknowledge worker request."); break; }
  }
  free(audio); free(output); return result;
}
int main(int argc, char **argv) {
  const char *audio=NULL,*model_path=NULL,*output=NULL,*language="en",*backend="cpu";
  int threads=4; pid_t worker_parent=0;
  for(int i=1;i<argc;i++) {
    if(!strcmp(argv[i],"--help")) { puts("parakeet-transcribe --model GGUF --language en --threads N --backend cpu|gpu (--audio WAV --output JSON | --worker-parent PID)"); return 0; }
    if(i+1>=argc) return fail("Every option requires a value; use --help.");
    const char *key=argv[i],*value=argv[++i];
    if(!strcmp(key,"--audio")) audio=value;
    else if(!strcmp(key,"--model")) model_path=value;
    else if(!strcmp(key,"--output")) output=value;
    else if(!strcmp(key,"--language")) language=value;
    else if(!strcmp(key,"--backend")) backend=value;
    else if(!strcmp(key,"--threads")) { char *end; errno=0; long n=strtol(value,&end,10); if(errno||*end||n<1||n>1024) return fail("Threads must be an integer from 1 to 1024."); threads=(int)n; }
    else if(!strcmp(key,"--worker-parent")) {
      char *end; errno=0; long n=strtol(value,&end,10);
      if(errno||*end||n<1||n>INT_MAX) return fail("Worker parent must be a positive PID.");
      worker_parent=(pid_t)n;
      /* Register before model work; checking afterward closes the parent-exit race. */
      if(prctl(PR_SET_PDEATHSIG,SIGKILL) || getppid()!=worker_parent) return fail("Cannot bind worker lifetime to its parent.");
    }
    else return fail("Unknown option; use --help.");
  }
  if(!model_path || (worker_parent?(audio||output):(!audio||!output||!*output))) return fail("Provide --model and either --audio/--output or --worker-parent.");
  if(strcmp(language,"en") && strcmp(language,"en-US") && strcmp(language,"en-GB") && strcmp(language,"en_US") && strcmp(language,"en_GB")) return fail("Multitalker Parakeet supports English only; select en, en-US or en-GB.");
  if(strcmp(backend,"cpu") && strcmp(backend,"gpu")) return fail("Backend must be cpu or gpu; automatic fallback is disabled.");
  if(getenv("TRANSCRIBE_MULTITALKER_OFFLINE") || getenv("TRANSCRIBE_MULTITALKER_MODE") || getenv("TRANSCRIBE_MULTITALKER_REF_BG_COMPAT")) return fail("Unset TRANSCRIBE_MULTITALKER overrides; this bridge requires streaming kernel mode.");
  struct sigaction action={0}; action.sa_handler=stop; sigemptyset(&action.sa_mask);
  if(sigaction(SIGTERM,&action,NULL) || sigaction(SIGINT,&action,NULL)) return fail("Cannot install cancellation handlers.");
  FILE *protocol=NULL;
  if(worker_parent) {
    int fd=dup(STDOUT_FILENO);
    if(fd<0) return fail("Cannot preserve worker protocol output.");
    protocol=fdopen(fd,"w");
    if(!protocol) { close(fd); return fail("Cannot open worker protocol output."); }
    if(dup2(STDERR_FILENO,STDOUT_FILENO)<0) { fclose(protocol); return fail("Cannot redirect native diagnostics."); }
  }
  int result=1;
  struct transcribe_model *model=NULL;
  if(!configure_threads(threads)) { fail("Could not configure CPU thread limits."); goto done; }
  if(strcmp(transcribe_version(),"0.2.3")) { fail("Runtime must be transcribe.cpp 0.2.3; rerun linux/setup-parakeet.sh."); goto done; }
  if(!model_integrity(model_path)) { fail("Model must be the hash-verified pinned four-speaker Q8 bundle; rerun linux/setup-parakeet.sh."); goto done; }
  struct transcribe_model_load_params load; transcribe_model_load_params_init(&load);
  if(transcribe_init_backends_default()!=TRANSCRIBE_OK) { fail("Runtime backend modules are unavailable; rerun linux/setup-parakeet.sh."); goto done; }
  load.backend=TRANSCRIBE_BACKEND_CPU;
  if(!strcmp(backend,"gpu")) {
    if(transcribe_backend_available(TRANSCRIBE_BACKEND_CUDA)) load.backend=TRANSCRIBE_BACKEND_CUDA;
    else if(transcribe_backend_available(TRANSCRIBE_BACKEND_VULKAN)) load.backend=TRANSCRIBE_BACKEND_VULKAN;
    else { fail("GPU requested but no CUDA/Vulkan backend is available; install a compatible driver/runtime or explicitly select CPU."); goto done; }
  }
  transcribe_status status=cancelled?TRANSCRIBE_ERR_ABORTED:transcribe_model_load_file(model_path,&load,&model);
  if(status!=TRANSCRIBE_OK) { fprintf(stderr,"Model load failed (status %d); verify the bundled GGUF and backend.\n",status); goto done; }
  if(strcmp(transcribe_model_variant_string(model),"multitalker-parakeet-streaming-0.6b-v1") || !transcribe_model_supports(model,TRANSCRIBE_FEATURE_DIARIZATION)) { fail("Wrong model: requires Multitalker Parakeet with embedded four-speaker diarizer."); goto done; }
  struct transcribe_device_info active; transcribe_device_info_init(&active);
  if(transcribe_device_get_info(transcribe_model_device(model), &active)!=TRANSCRIBE_OK || (!strcmp(backend,"cpu") && active.device_type!=TRANSCRIBE_DEVICE_TYPE_CPU) || (!strcmp(backend,"gpu") && active.device_type!=TRANSCRIBE_DEVICE_TYPE_GPU && active.device_type!=TRANSCRIBE_DEVICE_TYPE_IGPU)) { fail("Requested backend is not active; no fallback permitted."); goto done; }
  if(!cancelled) result=worker_parent?worker_loop(protocol,model,model_path,threads,backend):run_request(model,model_path,audio,output,threads,backend);
 done:
  transcribe_model_free(model); if(protocol) fclose(protocol); return cancelled?130:result;
}
