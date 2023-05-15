#include <c10/util/RefcountedDeleter.h>

#include <mutex>

namespace c10 {

void refcounted_deleter(void* ctx_) {
  RefcountedDeleterContext& ctx =
      *reinterpret_cast<RefcountedDeleterContext*>(ctx_);
  ctx.refcount--;
  if (ctx.refcount == 0) {
    ctx.other_ctx = nullptr;
    delete &ctx;
  }
}

std::mutex replace_data_ptr_mutex;

void maybeApplyRefcountedDeleter(c10::Storage storage) {
  std::lock_guard<std::mutex> guard(replace_data_ptr_mutex);
  c10::DataPtr& data_ptr = storage.mutable_data_ptr();

  c10::DeleterFnPtr deleter_expected = &c10::refcounted_deleter;
  c10::DeleterFnPtr deleter_current = data_ptr.get_deleter();

  if ((void*)data_ptr.get_deleter() == (void*)&c10::refcounted_deleter) {
    // Data pointer is already shared
    return;
  }

  void* data = data_ptr.get();
  void* other_ctx = data_ptr.get_context();
  c10::DeleterFnPtr other_deleter = data_ptr.get_deleter();
  c10::Device device = data_ptr.device();

  // Release the context of the original DataPtr so that the data doesn't
  // get deleted when the original DataPtr is replaced
  data_ptr.release_context();

  c10::RefcountedDeleterContext* refcount_ctx =
      new c10::RefcountedDeleterContext(other_ctx, other_deleter);

  c10::DataPtr new_data_ptr(
      data,
      reinterpret_cast<void*>(refcount_ctx),
      &c10::refcounted_deleter,
      device);
  storage.set_data_ptr(std::move(new_data_ptr));
}

c10::Storage newStorageImplFromRefcountedDataPtr(c10::Storage storage) {
  c10::maybeApplyRefcountedDeleter(storage);

  c10::StorageImpl* storage_impl = storage.unsafeGetStorageImpl();

  c10::DataPtr& data_ptr = storage.mutable_data_ptr();
  c10::DataPtr new_data_ptr(
      data_ptr.get(),
      data_ptr.get_context(),
      data_ptr.get_deleter(),
      data_ptr.device());

  reinterpret_cast<c10::RefcountedDeleterContext*>(data_ptr.get_context())
      ->refcount++;

  c10::Allocator* allocator = c10::GetAllocator(storage_impl->device_type());
  c10::Storage new_storage = c10::make_intrusive<c10::StorageImpl>(
      c10::StorageImpl::use_byte_size_t(),
      storage_impl->nbytes(),
      allocator,
      /*resizable=*/storage_impl->resizable());
  new_storage.set_data_ptr(std::move(new_data_ptr));
  return new_storage;
}

} // namespace c10
